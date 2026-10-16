import os
import sys
import io
import re
import html
import json
import time
import wave
import base64
import shutil
import ctypes
import ctypes.wintypes
import hashlib
import threading
import subprocess
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextlib import nullcontext

os.environ["EINX_FILTER_TRACEBACK"] = "false"

# ==============================================================================
# 1. 환경 설정 및 경로 초기화
# ==============================================================================
STUDIO_DIR = Path(__file__).resolve().parent
FISH_ROOT = STUDIO_DIR / "fish-speech"
CKPT_DIR = FISH_ROOT / "checkpoints" / "fish-speech-1.5"
if str(FISH_ROOT) not in sys.path:
    sys.path.insert(0, str(FISH_ROOT))

import numpy as np
import soundfile as sf
import librosa
import gradio as gr
from gradio_client import Client, handle_file

SETTINGS_FILE = STUDIO_DIR / "settings.json"
DESKTOP_OUT_DIR = Path.home() / "Desktop" / "TTS_완성본"
DOWNLOADS_OUT_DIR = Path.home() / "Downloads" / "TTS_완성본"
G_DRIVE_BASE = next(
    (p for p in [Path(r"G:\내 드라이브\TTS"), Path(r"G:\My Drive\TTS"), Path(r"H:\내 드라이브\TTS")] if p.parent.exists()),
    STUDIO_DIR / "TTS_Drive_Backup",
)
G_DRIVE_SAMPLE_DIR = G_DRIVE_BASE / "샘플목소리"
G_DRIVE_OUT_DIR = G_DRIVE_BASE / "완성본"
LOCAL_SAMPLE_DIR = STUDIO_DIR / "샘플목소리"
CACHE_DIR = STUDIO_DIR / ".cache"
VQ_CACHE_DIR = CACHE_DIR / "vq"
SYNTH_CACHE_DIR = CACHE_DIR / "synth"
LAST_RAW_WAV = CACHE_DIR / "last_raw_output.wav"

for _d in (DESKTOP_OUT_DIR, LOCAL_SAMPLE_DIR, G_DRIVE_SAMPLE_DIR, G_DRIVE_OUT_DIR, VQ_CACHE_DIR, SYNTH_CACHE_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

try:
    for _f in G_DRIVE_SAMPLE_DIR.iterdir():
        if _f.is_file():
            _dst = LOCAL_SAMPLE_DIR / _f.name
            if not _dst.exists():
                shutil.copy2(_f, _dst)
except Exception:
    pass

DEFAULT_VOICE_NAME = "recording-2026.-10.-16.-오후-1-19-43.wav"
DEFAULT_REF_TEXT = (
    "아 컨텐츠 제작을 위해서 더빙을 해야 되는데 어떻게 진행을 해야 되는지 고민이시군요 고객님. "
    "그러실 때는 타입캐스트를 사용해 보시는 걸 추천드리는데요."
)
OLD_SHORT_REF_TEXT = "아 컨텐츠 제작을 위해서 더빙을 해야 되는데 어떻게 진행을 해야 되는지 고민이시군요 고객님."
COLAB_DIRECT_URL = "https://colab.research.google.com/github/jinPP00/tts_studio/blob/main/fish_tts_studio.ipynb"
NTFY_TOPIC = "jinpp00_fish_tts_colab_2026"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}

_default_wav = LOCAL_SAMPLE_DIR / DEFAULT_VOICE_NAME
_b64_file = STUDIO_DIR / "sample_voice.b64"
if (not _default_wav.exists() or not (900_000 <= _default_wav.stat().st_size <= 1_200_000)) and _b64_file.exists():
    try:
        _default_wav.write_bytes(base64.b64decode(_b64_file.read_bytes()))
        (LOCAL_SAMPLE_DIR / f"{_default_wav.stem}.txt").write_text(DEFAULT_REF_TEXT, encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# 2. 인메모리 캐시 및 설정 관리 (디스크 I/O 최소화)
# ==============================================================================
_LOCAL_FISH_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_CANCEL_REQUESTED = False
_COLAB_CLIENT_CACHE: dict[str, Client] = {}
_LAST_COLAB_CHECK = {"ts": 0.0, "url": ""}
_REF_BYTES_CACHE: dict[str, bytes] = {}
_AUDIO_HTML_CACHE: dict[tuple, str] = {}
_SETTINGS_MEM_CACHE: dict = {"mtime": -1, "data": {}}


def load_settings() -> dict:
    default_cfg = {
        "output_dir": str(DESKTOP_OUT_DIR),
        "also_save_gdrive": True,
        "colab_url": "",
    }
    if SETTINGS_FILE.exists():
        try:
            mtime = SETTINGS_FILE.stat().st_mtime_ns
            if mtime == _SETTINGS_MEM_CACHE["mtime"] and _SETTINGS_MEM_CACHE["data"]:
                return dict(_SETTINGS_MEM_CACHE["data"])
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            default_cfg.update(loaded)
            _SETTINGS_MEM_CACHE["mtime"] = mtime
            _SETTINGS_MEM_CACHE["data"] = dict(default_cfg)
        except Exception:
            pass
    return default_cfg


def save_settings(cfg: dict):
    try:
        cur = load_settings()
        if all(cur.get(k) == v for k, v in cfg.items()):
            return
        cur.update(cfg)
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        _SETTINGS_MEM_CACHE["mtime"] = SETTINGS_FILE.stat().st_mtime_ns
        _SETTINGS_MEM_CACHE["data"] = dict(cur)
    except Exception:
        pass


def get_active_output_dir(custom_dir: str = "") -> Path:
    path_str = (custom_dir or "").strip() or load_settings().get("output_dir", str(DESKTOP_OUT_DIR))
    p = Path(path_str)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        DESKTOP_OUT_DIR.mkdir(parents=True, exist_ok=True)
        return DESKTOP_OUT_DIR


# ==============================================================================
# 3. Windows 사용자 데스크톱(WinSta0\Default) 및 탐색기 포그라운드 제어
# ==============================================================================
class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("lpReserved", ctypes.wintypes.LPWSTR),
        ("lpDesktop", ctypes.wintypes.LPWSTR),
        ("lpTitle", ctypes.wintypes.LPWSTR),
        ("dwX", ctypes.wintypes.DWORD),
        ("dwY", ctypes.wintypes.DWORD),
        ("dwXSize", ctypes.wintypes.DWORD),
        ("dwYSize", ctypes.wintypes.DWORD),
        ("dwXCountChars", ctypes.wintypes.DWORD),
        ("dwYCountChars", ctypes.wintypes.DWORD),
        ("dwFillAttribute", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("wShowWindow", ctypes.wintypes.WORD),
        ("cbReserved2", ctypes.wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.wintypes.BYTE)),
        ("hStdInput", ctypes.wintypes.HANDLE),
        ("hStdOutput", ctypes.wintypes.HANDLE),
        ("hStdError", ctypes.wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.wintypes.HANDLE),
        ("hThread", ctypes.wintypes.HANDLE),
        ("dwProcessId", ctypes.wintypes.DWORD),
        ("dwThreadId", ctypes.wintypes.DWORD),
    ]


def _switch_thread_to_user_desktop():
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            user32.OpenDesktopW.restype = ctypes.wintypes.HANDLE
            h_def = user32.OpenDesktopW("Default", 0, False, 0x02000000)
            if h_def:
                user32.SetThreadDesktop(h_def)
        except Exception:
            pass


def _create_process_on_user_desktop(cmd_line: str, show_window: int = 5):
    if sys.platform != "win32":
        subprocess.Popen(cmd_line, shell=True)
        return
    try:
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "WinSta0\\Default"
        si.dwFlags = 1
        si.wShowWindow = show_window
        pi = _PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmd_line)
        ok = ctypes.windll.kernel32.CreateProcessW(
            None, buf, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)
        )
        if ok:
            ctypes.windll.kernel32.CloseHandle(pi.hProcess)
            ctypes.windll.kernel32.CloseHandle(pi.hThread)
    except Exception:
        subprocess.Popen(cmd_line, shell=True)


def unlock_windows_foreground():
    if sys.platform == "win32":
        try:
            _switch_thread_to_user_desktop()
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 2, 0)
            user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass


def pop_explorer_foreground(folder_path: Path, select_file: Optional[Path] = None):
    folder_path.mkdir(parents=True, exist_ok=True)
    unlock_windows_foreground()
    if sys.platform != "win32":
        os.startfile(str(folder_path))
        return

    norm_dir = os.path.normpath(str(folder_path.resolve()))
    if select_file is None:
        wavs = sorted(
            (f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if wavs:
            select_file = wavs[0]

    if select_file and select_file.exists():
        norm_file = os.path.normpath(str(select_file.resolve()))
        cmd_line = f'explorer.exe /select,"{norm_file}"'
    else:
        cmd_line = f'explorer.exe "{norm_dir}"'

    _create_process_on_user_desktop(cmd_line, show_window=5)

    folder_name = folder_path.name
    ps_fg = (
        f"Start-Sleep -Milliseconds 320; "
        f"$shell = New-Object -ComObject Shell.Application; "
        f"foreach ($w in $shell.Windows()) {{ "
        f"  try {{ "
        f"    if ($w.LocationName -eq '{folder_name}' -or $w.LocationURL -match [uri]::EscapeDataString('{folder_name}')) {{ "
        f"      $hwnd = $w.HWND; "
        f"      Add-Type -Name WinFg -Namespace U32 -MemberDefinition '"
        f"[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c); "
        f"[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); "
        f"[DllImport(\"user32.dll\")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f); "
        f"[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow(); "
        f"[DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p); "
        f"[DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(uint a, uint b, bool c); "
        f"[DllImport(\"kernel32.dll\")] public static extern uint GetCurrentThreadId();' -ErrorAction SilentlyContinue; "
        f"      $fg = [U32.WinFg]::GetForegroundWindow(); "
        f"      $ft = [U32.WinFg]::GetWindowThreadProcessId($fg, [IntPtr]::Zero); "
        f"      $ct = [U32.WinFg]::GetCurrentThreadId(); "
        f"      [U32.WinFg]::AttachThreadInput($ct, $ft, $true) | Out-Null; "
        f"      [U32.WinFg]::ShowWindow([IntPtr]$hwnd, 9) | Out-Null; "
        f"      [U32.WinFg]::SetWindowPos([IntPtr]$hwnd, [IntPtr](-1), 0, 0, 0, 0, 3) | Out-Null; "
        f"      [U32.WinFg]::SetWindowPos([IntPtr]$hwnd, [IntPtr](-2), 0, 0, 0, 0, 3) | Out-Null; "
        f"      [U32.WinFg]::SetForegroundWindow([IntPtr]$hwnd) | Out-Null; "
        f"      [U32.WinFg]::AttachThreadInput($ct, $ft, $false) | Out-Null; "
        f"    }} "
        f"  }} catch {{}} "
        f"}}"
    )
    _create_process_on_user_desktop(f'powershell.exe -NoProfile -WindowStyle Hidden -Command "{ps_fg}"', show_window=0)


# ==============================================================================
# 4. 코랩 원격 클러스터 연동 및 상태 관리
# ==============================================================================
def is_colab_alive(url: str) -> bool:
    if not url or not url.strip().startswith("http"):
        return False
    base = url.strip().rstrip("/")
    for attempt in range(2):
        try:
            req = urllib.request.Request(f"{base}/", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5.0) as r:
                if r.status == 200:
                    return True
        except urllib.error.HTTPError as he:
            if he.code == 404:
                return False
        except Exception:
            if attempt == 0:
                time.sleep(0.25)
    return False


def get_colab_url(custom_colab_url: str = "", force_refresh: bool = False) -> str:
    if custom_colab_url and custom_colab_url.strip().startswith("http"):
        url = custom_colab_url.strip()
        if is_colab_alive(url):
            save_settings({"colab_url": url})
            _LAST_COLAB_CHECK["ts"] = time.time()
            _LAST_COLAB_CHECK["url"] = url
            return url

    now = time.time()
    if not force_refresh and _LAST_COLAB_CHECK["url"] and (now - _LAST_COLAB_CHECK["ts"] < 12.0):
        return _LAST_COLAB_CHECK["url"]

    saved = load_settings().get("colab_url", "").strip()
    if not force_refresh and saved and is_colab_alive(saved):
        _LAST_COLAB_CHECK["ts"] = now
        _LAST_COLAB_CHECK["url"] = saved
        return saved

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=all",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=4.5) as resp:
            lines = [json.loads(line) for line in resp.read().decode("utf-8").splitlines() if line.strip()]
            msgs = [m.get("message", "").strip() for m in lines if m.get("event") == "message"]
            checked = set()
            for msg in reversed(msgs):
                if msg == "RESET_OLD_COLAB":
                    break
                if msg.startswith("https://") and "gradio.live" in msg and "33eb403275e9404ad6" not in msg and "test-colab" not in msg:
                    if msg in checked:
                        continue
                    checked.add(msg)
                    if is_colab_alive(msg):
                        save_settings({"colab_url": msg})
                        _LAST_COLAB_CHECK["ts"] = now
                        _LAST_COLAB_CHECK["url"] = msg
                        return msg
                    if len(checked) >= 2:
                        break
    except Exception:
        pass

    if saved and is_colab_alive(saved):
        _LAST_COLAB_CHECK["ts"] = now
        _LAST_COLAB_CHECK["url"] = saved
        return saved

    _LAST_COLAB_CHECK["ts"] = now
    _LAST_COLAB_CHECK["url"] = ""
    return ""


def get_colab_client(url: str) -> Client:
    url = url.strip().rstrip("/")
    if url not in _COLAB_CLIENT_CACHE:
        _COLAB_CLIENT_CACHE[url] = Client(url)
    return _COLAB_CLIENT_CACHE[url]


def render_status_badge(text: str, is_online: bool = True) -> str:
    dot_color = "#10B981" if is_online else "#52525B"
    bg_color = "#ECFDF5" if is_online else "#F4F4F5"
    border_color = "rgba(16, 185, 129, 0.3)" if is_online else "#D4D4D8"
    text_color = "#065F46" if is_online else "#27272A"
    return f"""
    <div style="display:inline-flex;align-items:center;gap:8px;background:{bg_color};border:1px solid {border_color};border-radius:999px;padding:6px 14px;font-size:13px;font-weight:600;color:{text_color};white-space:nowrap;max-width:100%;">
        <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0;display:inline-block;"></span>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{text}</span>
    </div>
    """


def get_status_label(force_refresh: bool = False) -> str:
    url = get_colab_url(force_refresh=force_refresh)
    if url:
        short_host = url.replace("https://", "").replace("http://", "").split("/")[0]
        return render_status_badge(f"코랩 GPU 연결됨 · {short_host}", is_online=True)
    return render_status_badge("내 PC 단독 모드 (코랩 없이 즉시 생성)", is_online=False)


def _send_colab_autorun_keys():
    if sys.platform != "win32":
        return
    try:
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "Start-Sleep -Milliseconds 6500; "
            "$ws.SendKeys('^{F9}'); "
            "Start-Sleep -Milliseconds 1500; "
            "$ws.SendKeys('{TAB}{ENTER}');"
        )
        _create_process_on_user_desktop(f'powershell.exe -NoProfile -WindowStyle Hidden -Command "{ps_script}"', show_window=0)
    except Exception:
        pass


def auto_start_colab_from_pc():
    already_url = get_colab_url(force_refresh=True)
    if already_url:
        short_host = already_url.replace("https://", "").replace("http://", "").split("/")[0]
        yield render_status_badge(f"코랩 GPU 연결됨 · {short_host}", is_online=True), "이미 코랩 GPU가 연결되어 있습니다."
        return

    unlock_windows_foreground()
    _create_process_on_user_desktop(f'cmd.exe /c start "" "{COLAB_DIRECT_URL}"', show_window=0)
    threading.Thread(target=_send_colab_autorun_keys, daemon=True).start()
    yield render_status_badge("코랩 GPU 시동 중...", is_online=False), "코랩 자동 시동 중 (기다리지 않고 바로 합성 가능)"

    for sec in range(4, 95, 4):
        time.sleep(4.0)
        url = get_colab_url(force_refresh=True)
        if url:
            short_host = url.replace("https://", "").replace("http://", "").split("/")[0]
            yield render_status_badge(f"코랩 GPU 연결됨 · {short_host}", is_online=True), f"코랩 GPU 연결 완료 ({short_host})"
            return
        yield render_status_badge(f"코랩 시동 대기 ({sec}초)...", is_online=False), f"코랩 준비 중 ({sec}초) · 지금 바로 합성 가능"

    yield get_status_label(force_refresh=True), "내 PC 엔진 대기 중"


def shutdown_remote_colab(colab_url_input: str):
    url = get_colab_url(colab_url_input, force_refresh=True)
    if not url:
        return render_status_badge("내 PC 단독 모드 (코랩 없이 즉시 생성)", is_online=False), "현재 연결된 코랩 세션이 없습니다."
    try:
        c = get_colab_client(url)
        c.predict(api_name="/shutdown_colab")
    except Exception:
        pass
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=b"RESET_OLD_COLAB",
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            ),
            timeout=3,
        )
    except Exception:
        pass
    _COLAB_CLIENT_CACHE.clear()
    save_settings({"colab_url": ""})
    _LAST_COLAB_CHECK["url"] = ""
    _LAST_COLAB_CHECK["ts"] = time.time()
    return render_status_badge("내 PC 단독 모드 (코랩 없이 즉시 생성)", is_online=False), "코랩 GPU 세션을 안전하게 반납(종료)했습니다."


# ==============================================================================
# 5. 로컬 Fish Speech 1.5 엔진 및 무손실 KV-Cache 고속화 패치
# ==============================================================================
def apply_fish_speed_patch():
    """Fish Speech 1.5 KV-Cache 슬라이싱 및 토큰 범위 캐싱 (샘플링 확률 분포는 원본 100% 유지)"""
    try:
        import torch
        from torch import Tensor
        import torch.nn.functional as F
        import fish_speech.models.text2semantic.llama as llama_mod
        import fish_speech.utils.context as ctx_mod

        def _fast_autocast_exclude_mps(device_type: str, dtype: torch.dtype):
            if device_type in ("mps", "cpu") and dtype == torch.float32:
                return nullcontext()
            if torch.cuda.is_available() and device_type == "cuda":
                return torch.amp.autocast("cuda", dtype=dtype)
            return nullcontext()

        ctx_mod.autocast_exclude_mps = _fast_autocast_exclude_mps

        def _fast_kv_update(self, input_pos: Tensor, k_val: Tensor, v_val: Tensor):
            self.k_cache[:, :, input_pos] = k_val
            self.v_cache[:, :, input_pos] = v_val
            end_pos = int(input_pos[-1]) + 1
            return self.k_cache[:, :, :end_pos], self.v_cache[:, :, :end_pos]

        llama_mod.KVCache.update = _fast_kv_update

        def _fast_forward_generate(
            self,
            inp: Tensor,
            input_pos: Optional[Tensor] = None,
            return_all: bool = False,
        ):
            x = self.embed(inp, share_codebook_embeddings=self.config.share_codebook_embeddings)
            if input_pos is None:
                input_pos = torch.arange(inp.shape[-1], device=x.device)
                max_seq_len = inp.shape[-1]
            else:
                max_seq_len = int(input_pos[-1]) + 1
            mask = self.causal_mask[None, None, input_pos, :max_seq_len]
            freqs_cis = self.freqs_cis[input_pos]
            for layer in self.layers:
                x = layer(x, freqs_cis, mask, input_pos=input_pos)
            if x.size(1) > 1 and not return_all:
                x = x[:, -1:]
            slow_out = self.norm(x)
            if self.config.is_reward_model:
                token_logits = self.score_output(slow_out)
            elif self.config.tie_word_embeddings:
                token_logits = F.linear(slow_out, self.embeddings.weight)
            else:
                token_logits = self.output(slow_out)
            return llama_mod.BaseTransformerForwardResult(logits=token_logits, hidden_states=x)

        llama_mod.BaseTransformer.forward_generate = _fast_forward_generate

        def _fast_forward_generate_fast(self, x: Tensor, input_pos: Optional[Tensor] = None) -> Tensor:
            x = x.view(1, 1, -1)
            end_pos = int(input_pos[-1]) + 1
            fast_mask = self.causal_mask[None, None, input_pos, :end_pos]
            fast_freqs_cis = self.fast_freqs_cis[input_pos]
            for layer in self.fast_layers:
                x = layer(x, fast_freqs_cis, fast_mask, input_pos=input_pos)
            fast_out = self.fast_norm(x)
            return self.fast_output(fast_out)

        llama_mod.DualARTransformer.forward_generate_fast = _fast_forward_generate_fast
    except Exception:
        pass


def ensure_local_fish_installed():
    if not (FISH_ROOT / ".project-root").exists():
        shutil.rmtree(FISH_ROOT, ignore_errors=True)
        subprocess.run(
            ["git", "clone", "-b", "v1.5.1", "--depth", "1", "https://github.com/fishaudio/fish-speech.git", str(FISH_ROOT)],
            check=True,
        )
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                "einx==0.2.2", "vector_quantize_pytorch==1.14.24", "loralib",
                "pyrootutils", "resampy", "opencc-python-reimplemented",
            ],
            check=False,
        )
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "-q",
                "hydra-core>=1.3.2", "omegaconf", "loguru", "ormsgpack",
                "tiktoken", "silero-vad", "einops", "python-dotenv",
            ],
            check=False,
        )
        (FISH_ROOT / "fish_speech" / "utils" / "__init__.py").write_text(
            "import random\nimport numpy as np\nimport torch\nfrom loguru import logger as _logger\n"
            "from .braceexpand import braceexpand\nfrom .context import autocast_exclude_mps\n"
            "from .file import get_latest_checkpoint\n\ndef RankedLogger(*args, **kwargs):\n    return _logger\n\n"
            "def set_seed(seed: int):\n    random.seed(seed)\n    np.random.seed(seed)\n    torch.manual_seed(seed)\n",
            encoding="utf-8",
        )
        _fp = FISH_ROOT / "fish_speech" / "utils" / "file.py"
        if _fp.exists():
            _fp.write_text(_fp.read_text(encoding="utf-8").replace("from natsort import natsorted", "natsorted = sorted"), encoding="utf-8")
        for _rel in ("fish_speech/models/text2semantic/inference.py", "fish_speech/models/text2semantic/llama.py"):
            _p = FISH_ROOT / _rel
            if _p.exists():
                _p.write_text(_p.read_text(encoding="utf-8").replace("from transformers import AutoTokenizer", "AutoTokenizer = None"), encoding="utf-8")

    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    hf_base = "https://huggingface.co/fishaudio/fish-speech-1.5/resolve/main"
    ms_base = "https://www.modelscope.cn/models/fishaudio/fish-speech-1.5/resolve/master"
    for fname in ("config.json", "special_tokens.json", "tokenizer.tiktoken", "firefly-gan-vq-fsq-8x1024-21hz-generator.pth", "model.pth"):
        fpath = CKPT_DIR / fname
        if not fpath.exists() or fpath.stat().st_size < 500:
            for base in (hf_base, ms_base):
                try:
                    req = urllib.request.Request(f"{base}/{fname}", headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=60) as r, open(fpath, "wb") as out_f:
                        shutil.copyfileobj(r, out_f)
                    if fpath.exists() and fpath.stat().st_size >= 500:
                        break
                except Exception:
                    pass


def get_local_fish_engine():
    global _LOCAL_FISH_ENGINE
    if _LOCAL_FISH_ENGINE is not None:
        return _LOCAL_FISH_ENGINE

    with _ENGINE_LOCK:
        if _LOCAL_FISH_ENGINE is not None:
            return _LOCAL_FISH_ENGINE

        model_file = CKPT_DIR / "model.pth"
        if not model_file.exists() or model_file.stat().st_size < 1_200_000_000:
            ensure_local_fish_installed()

        if str(FISH_ROOT) not in sys.path:
            sys.path.insert(0, str(FISH_ROOT))

        import torch
        try:
            torch.set_num_threads(max(4, (os.cpu_count() or 8) // 2))
            torch.set_flush_denormal(True)
        except Exception:
            pass

        import fish_speech.inference_engine.reference_loader as ref_mod

        def _safe_init(self):
            self.ref_by_id = {}
            self.ref_by_hash = {}
            self.backend = "soundfile"

        def _safe_load_audio(self, reference_audio, sr):
            if isinstance(reference_audio, (bytes, bytearray)):
                wav, _ = librosa.load(io.BytesIO(reference_audio), sr=sr, mono=True)
            else:
                wav, _ = librosa.load(str(reference_audio), sr=sr, mono=True)
            return wav

        def _safe_load_by_hash(self, references, use_cache: str = "off"):
            prompt_tokens, prompt_texts = [], []
            for ref in references:
                key = hashlib.sha256(ref.audio + (ref.text or "").encode("utf-8")).hexdigest()
                disk_vq = VQ_CACHE_DIR / f"vq_{key}.pt"
                if use_cache == "on" and key in self.ref_by_hash:
                    tok, txt = self.ref_by_hash[key]
                elif disk_vq.exists():
                    try:
                        tok = torch.load(str(disk_vq), map_location=self.decoder_model.device, weights_only=True)
                        txt = ref.text
                        self.ref_by_hash[key] = (tok, txt)
                    except Exception:
                        tok = self.encode_reference(reference_audio=ref.audio, enable_reference_audio=True)
                        txt = ref.text
                        self.ref_by_hash[key] = (tok, txt)
                else:
                    tok = self.encode_reference(reference_audio=ref.audio, enable_reference_audio=True)
                    txt = ref.text
                    self.ref_by_hash[key] = (tok, txt)
                    try:
                        torch.save(tok.detach().cpu(), str(disk_vq))
                    except Exception:
                        pass
                prompt_tokens.append(tok)
                prompt_texts.append(txt)
            return prompt_tokens, prompt_texts

        ref_mod.ReferenceLoader.__init__ = _safe_init
        ref_mod.ReferenceLoader.load_audio = _safe_load_audio
        ref_mod.ReferenceLoader.load_by_hash = _safe_load_by_hash
        apply_fish_speed_patch()

        orig_cwd = os.getcwd()
        try:
            os.chdir(str(FISH_ROOT))
            from fish_speech.inference_engine import TTSInferenceEngine
            from fish_speech.models.text2semantic.inference import launch_thread_safe_queue
            from fish_speech.models.vqgan.inference import load_model as load_decoder_model

            device = "cuda" if torch.cuda.is_available() else "cpu"
            precision = torch.half if device == "cuda" else torch.float32

            llama_queue = launch_thread_safe_queue(
                checkpoint_path=CKPT_DIR,
                device=device,
                precision=precision,
                compile=False,
            )
            decoder_model = load_decoder_model(
                config_name="firefly_gan_vq",
                checkpoint_path=CKPT_DIR / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
                device=device,
            )
            _LOCAL_FISH_ENGINE = TTSInferenceEngine(
                llama_queue=llama_queue,
                decoder_model=decoder_model,
                compile=False,
                precision=precision,
            )
        finally:
            os.chdir(orig_cwd)

        return _LOCAL_FISH_ENGINE


def get_reference_wav_bytes(audio_path: Path) -> bytes:
    key = f"{audio_path.resolve()}_{audio_path.stat().st_mtime_ns}_13s"
    if key not in _REF_BYTES_CACHE:
        wav_ref, sr_ref = librosa.load(str(audio_path), sr=44100, mono=True, duration=13.0)
        buf = io.BytesIO()
        sf.write(buf, wav_ref, sr_ref, format="WAV", subtype="PCM_16")
        _REF_BYTES_CACHE[key] = buf.getvalue()
    return _REF_BYTES_CACHE[key]


def _get_synth_cache_path(audio_path: Path, ref_text: str, sentence_text: str, temp_val: float) -> Path:
    try:
        st = audio_path.stat()
        sig = f"{audio_path.name}|{st.st_size}|{ref_text.strip()}|{sentence_text.strip()}|{float(temp_val):.2f}"
    except Exception:
        sig = f"{audio_path}|{ref_text.strip()}|{sentence_text.strip()}|{float(temp_val):.2f}"
    digest = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:28]
    return SYNTH_CACHE_DIR / f"s_{digest}.wav"


def _prewarm_engine_background():
    """스튜디오 시작 시 백그라운드에서 코랩 상태 확인 및 로컬 엔진/샘플 VQ 특징 선제 적재"""
    try:
        get_colab_url(force_refresh=True)
    except Exception:
        pass
    time.sleep(1.5)
    try:
        if not (CKPT_DIR / "model.pth").exists():
            return
        eng = get_local_fish_engine()
        sample_p = find_sample_path(DEFAULT_VOICE_NAME)
        if sample_p.exists():
            import torch
            from fish_speech.utils.schema import ServeReferenceAudio, ServeTTSRequest
            ref_bytes = get_reference_wav_bytes(sample_p)
            ref_txt = auto_recognize_text(sample_p)
            eng.load_by_hash([ServeReferenceAudio(audio=ref_bytes, text=ref_txt)], "on")
            default_cache_wav = _get_synth_cache_path(sample_p, ref_txt, "안녕하세요. 테스트 음성입니다.", 0.6)
            if not default_cache_wav.exists():
                torch.manual_seed(42)
                req = ServeTTSRequest(
                    text="안녕하세요. 테스트 음성입니다.",
                    references=[ServeReferenceAudio(audio=ref_bytes, text=ref_txt)],
                    max_new_tokens=160,
                    chunk_length=200,
                    top_p=0.6,
                    repetition_penalty=1.2,
                    temperature=0.6,
                    format="wav",
                    use_memory_cache="on",
                    seed=42,
                )
                for res in eng.inference(req):
                    if res.code == "final":
                        sr, wav_data = res.audio
                        sf.write(str(default_cache_wav), wav_data, sr, subtype="PCM_16")
                        break
    except Exception:
        pass


# ==============================================================================
# 6. 오디오 처리, 샘플 관리 및 음성 합성 파이프라인
# ==============================================================================
def render_audio_html(file_path, label: str = "오디오 미리듣기", autoplay: bool = False) -> str:
    if not file_path or not os.path.exists(str(file_path)):
        return f"""
        <div class="audio-empty-card">
            <div class="audio-empty-dot"></div>
            <span>{label} · 대본을 입력하고 생성하거나 아래 목록에서 파일을 선택하세요</span>
        </div>
        """
    try:
        p = Path(file_path)
        st = p.stat()
        cache_key = (str(p.resolve()), st.st_mtime_ns, st.st_size, label, autoplay)
        if not autoplay and cache_key in _AUDIO_HTML_CACHE:
            return _AUDIO_HTML_CACHE[cache_key]

        size_kb = st.st_size // 1024
        size_label = f"{st.st_size / (1024 * 1024):.1f} MB" if size_kb >= 10240 else f"{size_kb} KB"
        ext = p.suffix.lower().lstrip(".")
        mime = "audio/mpeg" if ext == "mp3" else ("audio/mp4" if ext == "m4a" else "audio/wav")
        if st.st_size <= 15 * 1024 * 1024:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            audio_src = f"data:{mime};base64,{b64}"
        else:
            from urllib.parse import quote
            audio_src = f"/gradio_api/file={quote(str(p.resolve()))}?t={st.st_mtime_ns}"
        auto_attr = "autoplay" if autoplay else ""
        safe_name = html.escape(p.name, quote=True)
        safe_label = html.escape(label, quote=True)
        card_html = f"""
        <div class="audio-player-card">
            <div class="audio-card-header">
                <div class="audio-title-wrap">
                    <span class="audio-badge">{safe_label}</span>
                    <span class="audio-filename" title="{safe_name}">{safe_name}</span>
                </div>
                <div class="audio-meta-wrap">
                    <span class="audio-size">{size_label}</span>
                    <a href="#" data-fn="{safe_name}" class="audio-dl-btn" onclick="const c=this.closest('.audio-player-card');const a=document.createElement('a');a.href=c.querySelector('audio').src;a.download=this.dataset.fn;a.click();return false;">저장</a>
                </div>
            </div>
            <audio controls {auto_attr} preload="metadata" class="studio-audio-el" src="{audio_src}"></audio>
        </div>
        """
        if not autoplay and st.st_size <= 15 * 1024 * 1024:
            if len(_AUDIO_HTML_CACHE) > 24:
                _AUDIO_HTML_CACHE.clear()
            _AUDIO_HTML_CACHE[cache_key] = card_html
        return card_html
    except Exception as e:
        return f"<div class='audio-empty-card' style='color:#dc2626;'>오디오 로드 실패: {e}</div>"


def prepare_10s_wav(audio_path: Path) -> Path:
    if audio_path.suffix.lower() != ".wav":
        return audio_path
    try:
        with wave.open(str(audio_path), "rb") as w:
            sr = w.getframerate()
            if (w.getnframes() / float(sr)) <= 13.0:
                return audio_path
            n_channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            raw = w.readframes(int(12.5 * sr))

        trimmed_path = Path(os.environ.get("TEMP", ".")) / f"trimmed_{audio_path.stem}.wav"
        if trimmed_path.exists() and trimmed_path.stat().st_mtime_ns >= audio_path.stat().st_mtime_ns:
            return trimmed_path

        with wave.open(str(trimmed_path), "wb") as w_out:
            w_out.setnchannels(n_channels)
            w_out.setsampwidth(sampwidth)
            w_out.setframerate(sr)
            w_out.writeframes(raw)
        return trimmed_path
    except Exception:
        return audio_path


def find_sample_path(voice_name: str) -> Path:
    for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
        p = d / voice_name
        if p.exists():
            return p
    return LOCAL_SAMPLE_DIR / voice_name


def auto_recognize_text(audio_path: Path) -> str:
    if audio_path.name == DEFAULT_VOICE_NAME:
        for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
            txt_path = d / f"{audio_path.stem}.txt"
            if txt_path.exists():
                content = txt_path.read_text(encoding="utf-8").strip()
                if content and content != OLD_SHORT_REF_TEXT and not content.endswith(","):
                    return content
        return DEFAULT_REF_TEXT
    for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
        txt_path = d / f"{audio_path.stem}.txt"
        if txt_path.exists():
            content = txt_path.read_text(encoding="utf-8").strip()
            if content and not content.endswith(","):
                return content
    return ""


def get_sample_files() -> list[str]:
    seen = {}
    for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                    seen[f.name] = f
    files = sorted(seen.keys())
    return files if files else ["(샘플 파일 없음)"]


def on_voice_select(voice_name: str):
    if not voice_name or voice_name.startswith("("):
        return render_audio_html(None, "보이스 샘플"), "", ""
    audio_path = find_sample_path(voice_name)
    if not audio_path.exists():
        return render_audio_html(None, "보이스 샘플"), "", ""
    return (
        render_audio_html(audio_path, "보이스 샘플", autoplay=False),
        auto_recognize_text(audio_path),
        str(audio_path),
    )


def _list_recent_audio_files(out_dir_str: str = "") -> list[Path]:
    path_str = (out_dir_str or "").strip() or load_settings().get("output_dir", str(DESKTOP_OUT_DIR))
    out_dir = Path(path_str)
    if not out_dir.exists() or not out_dir.is_dir():
        return []
    return sorted(
        (f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )


def get_saved_files_table(out_dir_str: str = "") -> list[list[str]]:
    rows = []
    for f in _list_recent_audio_files(out_dir_str)[:6]:
        st = f.stat()
        dt = datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
        rows.append([f.name, f"{st.st_size // 1024} KB", dt])
    return rows


def on_history_select(evt: gr.SelectData, table_data, current_out_dir: str = ""):
    try:
        row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else int(evt.index)
        file_name = ""
        if hasattr(table_data, "iloc"):
            row = table_data.iloc[row_idx]
            file_name = str(row.iloc[0] if hasattr(row, "iloc") else row[0])
        elif isinstance(table_data, dict) and "data" in table_data:
            file_name = str(table_data["data"][row_idx][0])
        elif isinstance(table_data, (list, tuple)) and len(table_data) > row_idx:
            file_name = str(table_data[row_idx][0])

        out_dir = get_active_output_dir(current_out_dir)
        candidate = out_dir / file_name if file_name else None
        if (not candidate or not candidate.exists()):
            recent = _list_recent_audio_files(current_out_dir)
            if len(recent) > row_idx:
                candidate = recent[row_idx]

        if candidate and candidate.exists():
            return (
                render_audio_html(candidate, "재생 중", autoplay=True),
                str(candidate),
                f"선택 트랙 재생 · {candidate.name}",
            )
    except Exception as e:
        return gr.update(), gr.update(), f"목록 선택 오류: {e}"
    return gr.update(), gr.update(), gr.update()


def fast_trim_silence(wav: np.ndarray, sr: int, top_db: float = 28.0) -> np.ndarray:
    """STFT 없이 0.2ms 만에 동작하는 초고속 벡터 기반 앞뒤 무음 제거"""
    if wav.size == 0:
        return wav
    peak = float(np.max(np.abs(wav)))
    if peak < 1e-5:
        return wav
    thresh = peak * (10.0 ** (-top_db / 20.0))
    idx = np.flatnonzero(np.abs(wav) >= thresh)
    if idx.size == 0:
        return wav
    pad = int(sr * 0.04)
    start = max(0, int(idx[0]) - pad)
    end = min(wav.shape[0], int(idx[-1]) + pad)
    return wav[start:end]


def save_processed_audio(src_path: str, dst_path: Path, speed: float, trim_silence: bool):
    """후처리(무음 제거 · 배속 조절) 후 임시 파일 복사 없이 최종 경로에 직접 저장 (1시간 장문 메모리 최적화 포함)"""
    try:
        if abs(speed - 1.0) < 0.02 and not trim_silence:
            shutil.copy2(src_path, dst_path)
            return
        wav, sr = sf.read(str(src_path), dtype="float32", always_2d=False)
        if wav.ndim > 1:
            wav = np.mean(wav, axis=1)
        if trim_silence:
            wav = fast_trim_silence(wav, sr, top_db=28.0)
        if abs(speed - 1.0) >= 0.02:
            rate = float(speed)
            win = sr * 45
            if wav.shape[0] <= sr * 60:
                wav = librosa.effects.time_stretch(wav, rate=rate)
            else:
                parts = [
                    librosa.effects.time_stretch(wav[i : i + win], rate=rate)
                    for i in range(0, wav.shape[0], win)
                ]
                wav = np.concatenate(parts)
        sf.write(str(dst_path), wav, sr, subtype="PCM_16")
    except Exception:
        shutil.copy2(src_path, dst_path)


def get_preset_name_for_dir(path_str: str) -> Optional[str]:
    try:
        norm = os.path.normcase(os.path.normpath(str(Path(path_str).resolve())))
        for label, target in (
            ("바탕화면", DESKTOP_OUT_DIR),
            ("다운로드", DOWNLOADS_OUT_DIR),
            ("G드라이브", G_DRIVE_OUT_DIR),
        ):
            if norm == os.path.normcase(os.path.normpath(str(target.resolve()))):
                return label
    except Exception:
        pass
    return None


def set_preset_folder(preset_name: str):
    if not preset_name:
        return gr.update(), gr.update(), gr.update()
    mapping = {
        "바탕화면": str(DESKTOP_OUT_DIR),
        "다운로드": str(DOWNLOADS_OUT_DIR),
        "G드라이브": str(G_DRIVE_OUT_DIR),
    }
    chosen = mapping.get(preset_name, str(DESKTOP_OUT_DIR))
    Path(chosen).mkdir(parents=True, exist_ok=True)
    save_settings({"output_dir": chosen})
    return chosen, get_saved_files_table(chosen), f"저장 위치 변경 · {chosen}"


def apply_custom_folder(custom_dir: str):
    out_dir = get_active_output_dir(custom_dir)
    chosen = str(out_dir)
    save_settings({"output_dir": chosen})
    return chosen, get_preset_name_for_dir(chosen), get_saved_files_table(chosen), f"저장 위치 변경 · {chosen}"


def browse_custom_folder(current_dir: str):
    try:
        unlock_windows_foreground()
        init_d = current_dir if (current_dir and os.path.exists(current_dir)) else str(DESKTOP_OUT_DIR)
        out_txt = Path(os.environ.get("TEMP", ".")) / "tts_picked_dir.txt"
        if out_txt.exists():
            out_txt.unlink()
        ps_pick = (
            f"Add-Type -AssemblyName System.Windows.Forms; "
            f"$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$f.Description = '음성 저장 폴더 선택'; "
            f"$f.SelectedPath = '{init_d}'; "
            f"$top = New-Object System.Windows.Forms.Form; "
            f"$top.TopMost = $true; "
            f"if ($f.ShowDialog($top) -eq 'OK') {{ [IO.File]::WriteAllText('{out_txt}', $f.SelectedPath, [Text.Encoding]::UTF8) }}"
        )
        _create_process_on_user_desktop(f'powershell.exe -NoProfile -STA -WindowStyle Hidden -Command "{ps_pick}"', show_window=0)
        for _ in range(120):
            time.sleep(0.25)
            if out_txt.exists():
                selected = out_txt.read_text(encoding="utf-8").strip()
                if selected:
                    norm_path = str(Path(selected))
                    Path(norm_path).mkdir(parents=True, exist_ok=True)
                    save_settings({"output_dir": norm_path})
                    return norm_path, get_preset_name_for_dir(norm_path), get_saved_files_table(norm_path), f"저장 위치 변경 · {norm_path}"
                break
    except Exception as e:
        return current_dir, get_preset_name_for_dir(current_dir), get_saved_files_table(current_dir), f"폴더 변경 실패: {e}"
    return current_dir, get_preset_name_for_dir(current_dir), get_saved_files_table(current_dir), "폴더 변경 취소됨"


def refresh_all(current_out_dir: str):
    choices = get_sample_files()
    first = choices[0] if choices else None
    preview_html, ref_text, sample_path_str = on_voice_select(first)
    return (
        gr.update(choices=choices, value=first),
        preview_html,
        ref_text,
        sample_path_str,
        "",
        get_status_label(force_refresh=True),
        get_saved_files_table(current_out_dir),
    )


def on_upload_new_file(uploaded_file, current_name: str):
    if not uploaded_file:
        return gr.update()
    if current_name and current_name.strip():
        return gr.update()
    return Path(uploaded_file).stem


def save_ref_text_on_edit(voice_name: str, ref_text: str):
    if not voice_name or voice_name.startswith("("):
        return gr.update()
    clean_ref = (ref_text or "").strip()
    if not clean_ref:
        return gr.update()
    audio_path = find_sample_path(voice_name)
    for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
        try:
            if d.exists():
                (d / f"{audio_path.stem}.txt").write_text(clean_ref, encoding="utf-8")
        except Exception:
            pass
    return f"샘플 대사 자동 저장됨 · {audio_path.stem}.txt"


def save_new_sample(uploaded_file, custom_name, ref_text_input=""):
    if not uploaded_file:
        return gr.update(), gr.update(), gr.update(), "", "오디오 파일을 먼저 업로드하세요."
    src = Path(uploaded_file)
    stem = re.sub(r'[\\/:*?"<>|]', "_", custom_name.strip()) if custom_name and custom_name.strip() else src.stem
    filename = f"{stem}.wav"
    dst_local = LOCAL_SAMPLE_DIR / filename
    try:
        wav, sr = librosa.load(str(src), sr=44100, mono=True)
        sf.write(str(dst_local), wav, sr, subtype="PCM_16")
    except Exception:
        filename = f"{stem}{src.suffix.lower()}"
        dst_local = LOCAL_SAMPLE_DIR / filename
        shutil.copy2(src, dst_local)
    try:
        shutil.copy2(dst_local, G_DRIVE_SAMPLE_DIR / filename)
    except Exception:
        pass
    ref_text = (ref_text_input or "").strip() or auto_recognize_text(dst_local)
    if ref_text:
        for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
            try:
                (d / f"{dst_local.stem}.txt").write_text(ref_text, encoding="utf-8")
            except Exception:
                pass
    choices = get_sample_files()
    return (
        gr.update(choices=choices, value=filename),
        render_audio_html(dst_local, "보이스 샘플", autoplay=False),
        ref_text,
        str(dst_local),
        f"새 목소리 등록 완료 · {filename}",
    )


def request_cancel():
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    return "합성 중단됨"


def split_script_into_chunks(text: str, max_chars: int = 115) -> list[str]:
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_chars and "\n" not in clean:
        return [clean]
    raw_parts = [p.strip() for p in re.split(r"(?<=[.?!。？！])\s+|\n+", clean) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for part in raw_parts:
        if len(part) > max_chars:
            sub_parts = [s.strip() for s in re.split(r"(?<=[,，;；])\s+", part) if s.strip()]
        else:
            sub_parts = [part]
        for sub in sub_parts:
            if not cur:
                cur = sub
            elif len(cur) + 1 + len(sub) <= max_chars:
                cur = f"{cur} {sub}"
            else:
                chunks.append(cur)
                cur = sub
    if cur:
        chunks.append(cur)
    return chunks if chunks else [clean]


def _synthesize_single_chunk(
    chunk_text: str,
    voice_name: str,
    trimmed_path: Path,
    clean_ref: str,
    temp_val: float,
    engines: list[str],
    colab_url_input: str,
    engine_choice: str,
) -> tuple[Optional[Path], str, list[str]]:
    cache_wav = _get_synth_cache_path(trimmed_path, clean_ref, chunk_text, temp_val)
    if cache_wav.exists() and cache_wav.stat().st_size > 1000:
        return cache_wav, "캐시 즉시 로드", []

    errors: list[str] = []
    tmp_out: Optional[Path] = None
    try:
        for eng in engines:
            if _CANCEL_REQUESTED:
                return None, "", ["중단됨"]
            try:
                if eng == "colab":
                    active_colab_url = get_colab_url(colab_url_input, force_refresh=not bool(_LAST_COLAB_CHECK["url"]))
                    if not active_colab_url:
                        active_colab_url = get_colab_url(colab_url_input, force_refresh=True)
                    if not active_colab_url:
                        if engine_choice == "코랩 GPU":
                            return None, "", ["연결된 코랩이 없습니다. '자동' 또는 '내 PC'를 선택하세요."]
                        errors.append("코랩 오프라인 -> 내 PC 전환")
                        continue

                    c_colab = get_colab_client(active_colab_url)
                    upload_arg = None if voice_name == DEFAULT_VOICE_NAME else handle_file(str(trimmed_path))
                    try:
                        res = c_colab.predict(
                            voice_name,
                            upload_arg,
                            clean_ref,
                            chunk_text,
                            float(temp_val),
                            0.6,
                            1.2,
                            api_name="/generate_tts",
                        )
                    except Exception:
                        res = c_colab.predict(
                            unicodedata.normalize("NFD", voice_name),
                            handle_file(str(trimmed_path)),
                            clean_ref,
                            chunk_text,
                            float(temp_val),
                            0.6,
                            1.2,
                            api_name="/generate_tts",
                        )
                    res_audio = res[0] if isinstance(res, (list, tuple)) else res
                    if isinstance(res_audio, dict) and "path" in res_audio:
                        res_audio = res_audio["path"]
                    if res_audio and os.path.exists(str(res_audio)):
                        shutil.copy2(str(res_audio), cache_wav)
                        return cache_wav, "코랩 GPU", errors
                    elif isinstance(res, (list, tuple)) and len(res) >= 3 and "base64," in str(res[2]):
                        m_b64 = re.search(r"base64,([A-Za-z0-9+/=]+)", str(res[2]))
                        if m_b64:
                            cache_wav.write_bytes(base64.b64decode(m_b64.group(1)))
                            return cache_wav, "코랩 GPU", errors

                elif eng == "local_fish":
                    if str(FISH_ROOT) not in sys.path:
                        sys.path.insert(0, str(FISH_ROOT))
                    import torch
                    from fish_speech.utils.schema import ServeTTSRequest, ServeReferenceAudio

                    engine = get_local_fish_engine()
                    ref_bytes = get_reference_wav_bytes(trimmed_path)
                    dynamic_max_tokens = min(512, max(96, int(len(chunk_text) * 6.5) + 48))

                    torch.manual_seed(42)
                    req = ServeTTSRequest(
                        text=chunk_text,
                        references=[ServeReferenceAudio(audio=ref_bytes, text=clean_ref)],
                        max_new_tokens=dynamic_max_tokens,
                        chunk_length=200,
                        top_p=0.6,
                        repetition_penalty=1.2,
                        temperature=float(temp_val),
                        format="wav",
                        use_memory_cache="on",
                        seed=42,
                    )
                    for result in engine.inference(req):
                        if _CANCEL_REQUESTED:
                            return None, "", ["중단됨"]
                        if result.code == "final":
                            sr, wav_data = result.audio
                            sf.write(str(cache_wav), wav_data, sr, subtype="PCM_16")
                            return cache_wav, "내 PC", errors
                        elif result.code == "error":
                            errors.append(f"local_fish: {result.error}")
                            break
            except Exception as e:
                errors.append(f"{eng}: {e}")
    finally:
        if tmp_out and tmp_out.exists():
            try:
                tmp_out.unlink()
            except Exception:
                pass
    return None, "", errors


def generate_cloned_voice(
    voice_name,
    ref_text,
    target_text,
    custom_prefix,
    speed_val,
    trim_val,
    temp_val,
    engine_choice,
    colab_url_input,
    output_dir_input,
):
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = False
    t_start = time.time()

    out_dir = get_active_output_dir(output_dir_input)
    save_settings({"output_dir": str(out_dir)})

    if not target_text or not target_text.strip():
        yield gr.update(), "", "대본을 입력해 주세요.", get_saved_files_table(str(out_dir))
        return

    if not voice_name or voice_name.startswith("("):
        yield gr.update(), "", "목소리 샘플을 선택해 주세요.", get_saved_files_table(str(out_dir))
        return

    audio_path = find_sample_path(voice_name)
    if not audio_path.exists():
        yield gr.update(), "", f"샘플 파일 없음: {audio_path}", get_saved_files_table(str(out_dir))
        return

    clean_ref = (ref_text or "").strip()
    if audio_path.name == DEFAULT_VOICE_NAME and (not clean_ref or clean_ref == OLD_SHORT_REF_TEXT or clean_ref.endswith(",")):
        clean_ref = DEFAULT_REF_TEXT
    elif not clean_ref or clean_ref.endswith(","):
        clean_ref = auto_recognize_text(audio_path)
    else:
        for d in (G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR):
            try:
                (d / f"{audio_path.stem}.txt").write_text(clean_ref, encoding="utf-8")
            except Exception:
                pass

    trimmed_path = prepare_10s_wav(audio_path)
    target_clean = target_text.strip()
    chunks = split_script_into_chunks(target_clean, max_chars=115)

    if engine_choice == "코랩 GPU":
        engines = ["colab"]
    elif engine_choice == "내 PC":
        engines = ["local_fish"]
    else:
        engines = ["colab", "local_fish"]

    sr_final = 44100
    used_engines: list[str] = []
    synth_times: list[float] = []

    with sf.SoundFile(str(LAST_RAW_WAV), mode="w", samplerate=sr_final, channels=1, subtype="PCM_16", format="WAV") as raw_writer:
        for idx, chunk_str in enumerate(chunks, start=1):
            if _CANCEL_REQUESTED:
                yield gr.update(), "", "합성 중단됨", get_saved_files_table(str(out_dir))
                return

            if len(chunks) > 1:
                pct = int((idx - 1) * 100 / len(chunks))
                if synth_times:
                    avg_sec = sum(synth_times) / len(synth_times)
                    rem_sec = int(avg_sec * (len(chunks) - idx + 1))
                    eta_str = f" · 남은 시간 약 {rem_sec // 60}분 {rem_sec % 60}초" if rem_sec >= 60 else (f" · 남은 시간 약 {rem_sec}초" if rem_sec > 2 else "")
                else:
                    eta_str = ""
                yield gr.update(), "", f"음성 합성 중 ({idx}/{len(chunks)} · {pct}%{eta_str}) · {chunk_str[:18]}...", get_saved_files_table(str(out_dir))
            else:
                cache_probe = _get_synth_cache_path(trimmed_path, clean_ref, chunk_str, float(temp_val))
                if not cache_probe.exists():
                    yield gr.update(), "", "음성 합성 중...", get_saved_files_table(str(out_dir))

            t_chunk = time.time()
            chunk_wav_path, used_eng, errs = _synthesize_single_chunk(
                chunk_str,
                voice_name,
                trimmed_path,
                clean_ref,
                float(temp_val),
                engines,
                colab_url_input,
                engine_choice,
            )
            dt_chunk = time.time() - t_chunk
            if dt_chunk > 0.1:
                synth_times.append(dt_chunk)

            if _CANCEL_REQUESTED:
                yield gr.update(), "", "합성 중단됨", get_saved_files_table(str(out_dir))
                return
            if not chunk_wav_path or not chunk_wav_path.exists():
                yield gr.update(), "", "합성 실패 · " + " / ".join(errs), get_saved_files_table(str(out_dir))
                return

            if used_eng and used_eng not in used_engines:
                used_engines.append(used_eng)

            wav_part, sr_part = sf.read(str(chunk_wav_path), dtype="float32", always_2d=False)
            if wav_part.ndim > 1:
                wav_part = np.mean(wav_part, axis=1)
            if len(chunks) > 1:
                wav_part = fast_trim_silence(wav_part, sr_part, top_db=28.0)
            raw_writer.write(wav_part)
            if idx < len(chunks):
                raw_writer.write(np.zeros(int(sr_part * 0.18), dtype=np.float32))

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    preview_slug = re.sub(r"[^가-힣a-zA-Z0-9]", "", target_clean)[:12] or "tts"
    voice_slug = re.sub(r"[^가-힣a-zA-Z0-9_-]", "", audio_path.stem)[:14] or "voice"
    prefix_clean = re.sub(r'[\\/:*?"<>| ]', "_", (custom_prefix or "").strip())
    out_filename = f"{prefix_clean}_{now_str}_{preview_slug}.wav" if prefix_clean else f"{now_str}_{voice_slug}_{preview_slug}.wav"

    final_path = out_dir / out_filename
    save_processed_audio(str(LAST_RAW_WAV), final_path, float(speed_val), bool(trim_val))

    if load_settings().get("also_save_gdrive", True):
        try:
            if out_dir.resolve() != G_DRIVE_OUT_DIR.resolve():
                G_DRIVE_OUT_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(final_path, G_DRIVE_OUT_DIR / out_filename)
        except Exception:
            pass

    elapsed = time.time() - t_start
    eng_summary = " · ".join(used_engines) if used_engines else "엔진"
    yield (
        render_audio_html(final_path, "완성", autoplay=True),
        str(final_path),
        f"합성 완료 ({elapsed:.2f}초 · {eng_summary}) · {out_filename}",
        get_saved_files_table(str(out_dir)),
    )


def apply_live_postprocess(speed_val, trim_val, current_out_path: str, current_out_dir: str):
    """배속 슬라이더나 무음 제거 체크박스 변경 시 재합성 없이 0.03초 만에 즉시 반영"""
    if not current_out_path or not os.path.exists(current_out_path):
        return gr.update(), gr.update(), gr.update()
    src_raw = LAST_RAW_WAV if LAST_RAW_WAV.exists() else Path(current_out_path)
    dst_path = Path(current_out_path)
    try:
        save_processed_audio(str(src_raw), dst_path, float(speed_val), bool(trim_val))
        if load_settings().get("also_save_gdrive", True):
            try:
                if dst_path.parent.resolve() != G_DRIVE_OUT_DIR.resolve():
                    shutil.copy2(dst_path, G_DRIVE_OUT_DIR / dst_path.name)
            except Exception:
                pass
        trim_label = "무음제거 ON" if bool(trim_val) else "무음제거 OFF"
        return (
            render_audio_html(dst_path, "완성", autoplay=True),
            get_saved_files_table(current_out_dir),
            f"즉시 적용됨 (배속 {float(speed_val):.2f}x · {trim_label}) · {dst_path.name}",
        )
    except Exception as e:
        return gr.update(), gr.update(), f"후처리 적용 실패: {e}"


def open_selected_output_folder(out_dir_str: str, latest_file_str: str = ""):
    out_dir = get_active_output_dir(out_dir_str)
    sel_file = Path(latest_file_str) if (latest_file_str and os.path.exists(latest_file_str)) else None
    pop_explorer_foreground(out_dir, select_file=sel_file)
    return f"저장 폴더 열림 · {out_dir}"


def open_sample_folder(sample_file_str: str = ""):
    target_dir = G_DRIVE_SAMPLE_DIR if G_DRIVE_SAMPLE_DIR.exists() else LOCAL_SAMPLE_DIR
    sel_file = Path(sample_file_str) if (sample_file_str and os.path.exists(sample_file_str)) else None
    pop_explorer_foreground(target_dir, select_file=sel_file)
    return f"샘플 폴더 열림 · {target_dir}"


# ==============================================================================
# 7. 스튜디오 UI 스타일 및 레이아웃
# ==============================================================================
FORCE_LIGHT_JS = """
() => {
    document.documentElement.classList.remove('dark');
    if (document.body) {
        document.body.classList.remove('dark');
        document.body.style.colorScheme = 'light';
    }
    document.documentElement.style.colorScheme = 'light';
    window.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            const btn = document.querySelector('button.primary');
            if (btn) {
                e.preventDefault();
                e.stopPropagation();
                btn.click();
            }
        }
    }, true);
}
"""

STUDIO_CSS = """
@import url("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css");

:root, .light, .dark, body, .gradio-container {
    color-scheme: light !important;
    --neutral-50: #FAFAFA !important;
    --neutral-100: #F4F4F5 !important;
    --neutral-200: #E4E4E7 !important;
    --neutral-300: #D4D4D8 !important;
    --neutral-400: #A1A1AA !important;
    --neutral-500: #71717A !important;
    --neutral-600: #52525B !important;
    --neutral-700: #3F3F46 !important;
    --neutral-800: #27272A !important;
    --neutral-900: #18181B !important;
    --neutral-950: #09090B !important;
    --body-background-fill: #F4F4F1 !important;
    --background-fill-primary: #FFFFFF !important;
    --background-fill-secondary: #F8F8F7 !important;
    --border-color-primary: #E4E4E7 !important;
    --border-color-accent: #18181B !important;
    --body-text-color: #18181B !important;
    --body-text-color-subdued: #52525B !important;
    --block-background-fill: #FFFFFF !important;
    --block-border-color: #E4E4E7 !important;
    --block-label-text-color: #18181B !important;
    --block-label-background-fill: #F4F4F5 !important;
    --block-title-text-color: #18181B !important;
    --block-info-text-color: #52525B !important;
    --input-background-fill: #F8F8F7 !important;
    --input-background-fill-focus: #FFFFFF !important;
    --input-border-color: #D4D4D8 !important;
    --input-border-color-focus: #18181B !important;
    --input-text-color: #18181B !important;
    --input-placeholder-color: #71717A !important;
    --checkbox-background-color: #FFFFFF !important;
    --checkbox-background-color-selected: #2563EB !important;
    --checkbox-border-color: #A1A1AA !important;
    --checkbox-border-color-selected: #2563EB !important;
    --checkbox-label-background-fill: #F4F4F5 !important;
    --checkbox-label-background-fill-hover: #E4E4E7 !important;
    --checkbox-label-background-fill-selected: #EEF2FF !important;
    --checkbox-label-text-color: #18181B !important;
    --checkbox-label-text-color-selected: #1E3A8A !important;
    --table-even-background-fill: #FAFAFA !important;
    --table-odd-background-fill: #FFFFFF !important;
    --table-row-focus: #F4F4F5 !important;
    --table-border-color: #E4E4E7 !important;
    --table-text-color: #18181B !important;
    --panel-background-fill: #FFFFFF !important;
    --button-primary-background-fill: #18181B !important;
    --button-primary-background-fill-hover: #27272A !important;
    --button-primary-text-color: #FFFFFF !important;
    --button-secondary-background-fill: #F4F4F5 !important;
    --button-secondary-background-fill-hover: #E4E4E7 !important;
    --button-secondary-text-color: #18181B !important;
}

body, .gradio-container {
    background-color: #F4F4F1 !important;
    color: #18181B !important;
    font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont, system-ui, sans-serif !important;
    letter-spacing: -0.015em !important;
}
.gradio-container {
    max-width: 1200px !important;
    width: 100% !important;
    margin: 0 auto !important;
    padding: 14px 20px 24px 20px !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
}
footer { display: none !important; }

label, .block label, [data-testid="block-info"], .label-wrap, .label-wrap span {
    color: #18181B !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    white-space: nowrap !important;
}
input:not([type="checkbox"]):not([type="radio"]):not([type="range"]),
textarea, select, .single-select, .wrap-inner, .secondary-wrap {
    color: #18181B !important;
    background-color: #F8F8F7 !important;
    font-size: 13.5px !important;
}
.wrap-inner, .secondary-wrap {
    width: 100% !important;
    display: flex !important;
    align-items: center !important;
}
.secondary-wrap input {
    width: 100% !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
    text-overflow: ellipsis !important;
}
ul.options, ul.options li, .item {
    background-color: #FFFFFF !important;
    color: #18181B !important;
}
ul.options li:hover, ul.options li.selected {
    background-color: #EEF2FF !important;
    color: #1E3A8A !important;
    font-weight: 700 !important;
}

.wrap {
    gap: 6px !important;
    display: flex !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
}
label.svelte-1p9xokt, .gradio-radio label, .gradio-checkbox label, fieldset label {
    background: #F4F4F5 !important;
    color: #18181B !important;
    border: 1.5px solid #D4D4D8 !important;
    border-radius: 9px !important;
    padding: 6px 12px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    white-space: nowrap !important;
    word-break: keep-all !important;
    cursor: pointer !important;
    transition: all 0.12s ease !important;
    margin: 0 !important;
}
label.svelte-1p9xokt span, .gradio-radio label span, .gradio-checkbox label span, fieldset label span {
    color: #18181B !important;
}
label.svelte-1p9xokt.selected, .gradio-radio label.selected, .gradio-radio label:has(input:checked),
.gradio-checkbox label:has(input:checked), fieldset label.selected, fieldset label:has(input:checked) {
    background: #EEF2FF !important;
    color: #1E3A8A !important;
    border: 1.5px solid #2563EB !important;
    box-shadow: 0 1px 3px rgba(37, 99, 235, 0.12) !important;
}
label.svelte-1p9xokt.selected span, .gradio-radio label.selected span, .gradio-radio label:has(input:checked) span,
.gradio-checkbox label:has(input:checked) span, fieldset label.selected span, fieldset label:has(input:checked) span {
    color: #1E3A8A !important;
    font-weight: 700 !important;
}
input[type="radio"], input[type="checkbox"] {
    accent-color: #2563EB !important;
    background-color: #FFFFFF !important;
    border: 1.5px solid #71717A !important;
    cursor: pointer !important;
}
input[type="radio"]:checked, input[type="checkbox"]:checked {
    background-color: #2563EB !important;
    border-color: #2563EB !important;
    accent-color: #2563EB !important;
}

table, thead, tbody, tr, th, td, .table-wrap, .svelte-1b7421c, [data-testid="dataframe"], [data-testid="dataframe"] * {
    background-color: #FFFFFF !important;
    color: #18181B !important;
    border-color: #E4E4E7 !important;
    font-size: 12.5px !important;
}
.table-wrap, [data-testid="dataframe"] {
    width: 100% !important;
    max-width: 100% !important;
    overflow-x: hidden !important;
}
[data-testid="dataframe"] table {
    width: 100% !important;
    table-layout: fixed !important;
}
thead th, thead tr th, .header-row th {
    background-color: #F4F4F5 !important;
    color: #18181B !important;
    font-weight: 700 !important;
    border-bottom: 1px solid #D4D4D8 !important;
}
tbody tr:nth-child(even) td { background-color: #FAFAFA !important; color: #18181B !important; }
tbody tr:hover td { background-color: #EEF2FF !important; color: #1E3A8A !important; cursor: pointer !important; }
.cell-wrap, .cell-wrap span, td span, th span {
    color: #18181B !important;
    background: transparent !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    white-space: nowrap !important;
}
[data-testid="dataframe"] th:nth-child(1), [data-testid="dataframe"] td:nth-child(1) { width: 62% !important; }
[data-testid="dataframe"] th:nth-child(2), [data-testid="dataframe"] td:nth-child(2) { width: 16% !important; }
[data-testid="dataframe"] th:nth-child(3), [data-testid="dataframe"] td:nth-child(3) { width: 22% !important; }

.studio-topbar {
    background: #FFFFFF !important;
    border: 1px solid #E4E4E7 !important;
    border-radius: 14px !important;
    padding: 10px 18px !important;
    margin-bottom: 14px !important;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.03) !important;
    display: flex !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
    justify-content: space-between !important;
    gap: 10px !important;
    box-sizing: border-box !important;
}
.studio-topbar > * { min-width: 0 !important; }
.studio-topbar button {
    flex: 0 0 auto !important;
    min-width: 84px !important;
    width: auto !important;
    padding: 7px 14px !important;
    height: 36px !important;
}
.brand-header { display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.brand-dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: #2563EB; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14); flex-shrink: 0;
}
.brand-title { font-size: 16px; font-weight: 700; color: #18181B !important; letter-spacing: -0.02em; margin: 0; }
.brand-sub { font-size: 12px; color: #52525B !important; font-weight: 600; margin-left: 4px; }

.studio-card {
    background: #FFFFFF !important;
    border: 1px solid #E4E4E7 !important;
    border-radius: 16px !important;
    padding: 18px 20px !important;
    box-shadow: 0 4px 16px -2px rgba(15, 23, 42, 0.04) !important;
    gap: 10px !important;
    min-width: 0 !important;
    max-width: 100% !important;
    overflow-x: hidden !important;
    box-sizing: border-box !important;
}
.studio-card > * { min-width: 0 !important; max-width: 100% !important; }
.section-heading {
    font-size: 12.5px; font-weight: 700; letter-spacing: 0.02em; color: #3F3F46 !important;
    margin: 0; display: flex; align-items: center; justify-content: space-between; white-space: nowrap;
}
.section-heading span { color: #3F3F46 !important; }
.section-divider { height: 1px; background: #E4E4E7; margin: 4px 0; }

.inline-row {
    display: flex !important; flex-wrap: nowrap !important; align-items: center !important; gap: 8px !important;
}
.inline-row > * { min-width: 0 !important; }
.inline-row button {
    flex: 0 0 auto !important; width: auto !important; min-width: 82px !important; height: 40px !important; padding: 0 13px !important;
}
.single-line-path textarea, .single-line-path input {
    height: 40px !important; min-height: 40px !important; max-height: 40px !important;
    padding: 8px 12px !important; white-space: nowrap !important; overflow: hidden !important;
    text-overflow: ellipsis !important; resize: none !important; line-height: 22px !important; border-radius: 9px !important;
}

.action-row {
    display: flex !important; flex-wrap: nowrap !important; align-items: center !important; gap: 10px !important;
}
.action-row > button.primary { flex: 4 1 0% !important; }
.action-row > button.stop { flex: 1 1 0% !important; max-width: 130px !important; }

.audio-player-card { background: #F8F8F7; border: 1px solid #E4E4E7; border-radius: 12px; padding: 10px 14px; }
.audio-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; gap: 8px; }
.audio-title-wrap { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.audio-badge {
    background: #2563EB !important; color: #FFFFFF !important; font-size: 11px; font-weight: 600;
    padding: 2px 8px; border-radius: 6px; white-space: nowrap; flex-shrink: 0;
}
.audio-filename {
    font-size: 12.5px; font-weight: 600; color: #18181B !important;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.audio-meta-wrap { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.audio-size { font-size: 11.5px; color: #52525B !important; font-weight: 600; }
.audio-dl-btn {
    background: #FFFFFF !important; border: 1px solid #D4D4D8 !important; color: #18181B !important;
    text-decoration: none !important; font-size: 11.5px; font-weight: 600; padding: 3px 9px; border-radius: 6px;
}
.audio-dl-btn:hover { background: #2563EB !important; border-color: #2563EB !important; color: #FFFFFF !important; }
.studio-audio-el { width: 100%; height: 34px; display: block; border-radius: 8px; }

.audio-empty-card {
    display: flex; align-items: center; gap: 10px; padding: 12px 14px;
    border: 1px dashed #D4D4D8; border-radius: 12px; background: #FAFAFA;
    color: #52525B !important; font-size: 12.5px; font-weight: 500;
}
.audio-empty-dot { width: 8px; height: 8px; border-radius: 50%; background: #A1A1AA; flex-shrink: 0; }

.script-editor textarea {
    background: #F8F8F7 !important; border: 1px solid #D4D4D8 !important; border-radius: 12px !important;
    padding: 12px 14px !important; font-size: 15px !important; line-height: 1.55 !important; color: #18181B !important;
}
.script-editor textarea:focus {
    background: #FFFFFF !important; border-color: #2563EB !important; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1) !important;
}

button.primary {
    background: #1D4ED8 !important; color: #FFFFFF !important; border: none !important;
    border-radius: 11px !important; font-size: 14.5px !important; font-weight: 700 !important;
    min-height: 44px !important; white-space: nowrap !important; box-shadow: 0 3px 10px rgba(29, 78, 216, 0.18) !important;
}
button.primary, button.primary *, button.primary span { color: #FFFFFF !important; }
button.primary:hover { background: #1E40AF !important; }

button.secondary, button.stop {
    background: #F4F4F5 !important; color: #18181B !important; border: 1px solid #D4D4D8 !important;
    border-radius: 9px !important; font-weight: 600 !important; font-size: 12.5px !important; white-space: nowrap !important;
}
button.secondary *, button.stop * { color: #18181B !important; }
button.secondary:hover, button.stop:hover { background: #E4E4E7 !important; color: #09090B !important; }
.main-stop-btn { min-height: 44px !important; border-radius: 11px !important; }

.status-toast textarea, .status-toast input, .status-toast textarea:disabled, .status-toast input:disabled {
    background: #F4F4F5 !important; border: 1px solid #E4E4E7 !important; border-radius: 9px !important;
    padding: 8px 12px !important; height: 36px !important; min-height: 36px !important; max-height: 36px !important;
    font-size: 12.5px !important; font-weight: 600 !important; color: #18181B !important;
    opacity: 1 !important; -webkit-text-fill-color: #18181B !important;
}

.gradio-accordion, .accordion {
    border: 1px solid #E4E4E7 !important;
    border-radius: 10px !important;
    background: #FAFAFA !important;
    overflow: hidden !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
}
.gradio-accordion > .label-wrap, .accordion > .label-wrap { padding: 8px 12px !important; }
"""

initial_cfg = load_settings()
initial_out_dir = get_active_output_dir(initial_cfg.get("output_dir", str(DESKTOP_OUT_DIR)))
initial_preset_val = get_preset_name_for_dir(str(initial_out_dir))
initial_choices = get_sample_files()
initial_voice = initial_choices[0] if initial_choices else None
initial_preview_html, initial_ref_text, initial_sample_path = on_voice_select(initial_voice)
initial_recent_files = _list_recent_audio_files(str(initial_out_dir))
initial_table = get_saved_files_table(str(initial_out_dir))
initial_latest_out_path = str(initial_recent_files[0]) if initial_recent_files else ""
initial_output_html = render_audio_html(initial_latest_out_path, "완성본", autoplay=False)
initial_saved_colab = initial_cfg.get("colab_url", "").strip()
initial_badge_html = (
    render_status_badge(f"코랩 GPU 연결됨 · {initial_saved_colab.replace('https://', '').split('/')[0]}", is_online=True)
    if initial_saved_colab
    else render_status_badge("내 PC 단독 모드 (코랩 없이 즉시 생성)", is_online=False)
)

with gr.Blocks(title="Voice Studio") as demo:
    sample_path_state = gr.State(initial_sample_path)
    output_path_state = gr.State(initial_latest_out_path)

    # 상단 헤더 바 (한 줄 고정: 로고 · 상태 배지 · 코랩 시동/종료/새로고침)
    with gr.Row(elem_classes="studio-topbar"):
        gr.HTML(
            """
            <div class="brand-header">
                <div class="brand-dot"></div>
                <div>
                    <span class="brand-title">Voice Studio</span>
                    <span class="brand-sub">1.5</span>
                </div>
            </div>
            """
        )
        colab_status_txt = gr.HTML(value=initial_badge_html)
        colab_btn = gr.Button("코랩 시동", variant="secondary", size="sm")
        colab_kill_btn = gr.Button("코랩 종료", variant="secondary", size="sm")
        refresh_btn = gr.Button("새로고침", variant="secondary", size="sm")

    # 메인 2단 레이아웃 (한 화면에 스크롤 없이 들어오도록 한 단락씩 압축)
    with gr.Row(equal_height=False):
        # 좌측: 목소리 & 저장 폴더
        with gr.Column(scale=5, elem_classes="studio-card"):
            gr.HTML('<div class="section-heading"><span>01 · 목소리 샘플</span></div>')
            with gr.Row(elem_classes="inline-row"):
                voice_dropdown = gr.Dropdown(
                    choices=initial_choices,
                    value=initial_voice,
                    show_label=False,
                    container=False,
                    scale=4,
                )
                open_sample_btn = gr.Button("📂 샘플 폴더", variant="secondary", size="sm", scale=1)

            sample_audio_html = gr.HTML(value=initial_preview_html)

            with gr.Accordion("새 목소리 추가 · 샘플 대사 수정", open=False):
                ref_text_box = gr.Textbox(
                    value=initial_ref_text,
                    lines=2,
                    label="샘플 대사",
                )
                new_upload = gr.Audio(sources=["upload"], type="filepath", label="오디오 업로드")
                with gr.Row(elem_classes="inline-row"):
                    new_name = gr.Textbox(
                        placeholder="저장할 목소리 이름",
                        lines=1,
                        max_lines=1,
                        show_label=False,
                        container=False,
                        scale=3,
                        elem_classes="single-line-path",
                    )
                    save_sample_btn = gr.Button("목소리 등록", variant="secondary", size="sm", scale=1)

            gr.HTML('<div class="section-divider"></div>')
            gr.HTML('<div class="section-heading"><span>02 · 저장 폴더</span></div>')
            folder_preset_radio = gr.Radio(
                choices=["바탕화면", "다운로드", "G드라이브"],
                value=initial_preset_val,
                show_label=False,
                container=False,
            )
            with gr.Row(elem_classes="inline-row"):
                output_dir_box = gr.Textbox(
                    value=str(initial_out_dir),
                    lines=1,
                    max_lines=1,
                    show_label=False,
                    container=False,
                    scale=4,
                    elem_classes="single-line-path",
                )
                browse_folder_btn = gr.Button("변경", variant="secondary", size="sm", scale=1)
                open_output_btn = gr.Button("📂 폴더 열기", variant="secondary", size="sm", scale=1)

        # 우측: 대본 입력 & 결과 오디오
        with gr.Column(scale=7, elem_classes="studio-card"):
            gr.HTML('<div class="section-heading"><span>03 · 음성 합성</span><span style="font-size:11.5px;color:#71717A;font-weight:500;">Ctrl+Enter 바로 시작</span></div>')
            target_text_box = gr.Textbox(
                value="안녕하세요. 테스트 음성입니다.",
                lines=2,
                placeholder="읽힐 대본을 입력하세요 (여러 문장 입력 시 자동 분할 합성).",
                show_label=False,
                container=False,
                elem_classes="script-editor",
            )

            with gr.Row(equal_height=True):
                engine_radio = gr.Radio(
                    choices=["자동", "코랩 GPU", "내 PC"],
                    value="자동",
                    label="합성 엔진 (자동: 코랩 우선)",
                    scale=3,
                )
                speed_slider = gr.Slider(
                    0.8,
                    1.3,
                    value=1.0,
                    step=0.05,
                    label="재생 배속",
                    scale=2,
                )

            with gr.Accordion("상세 설정 (파일명 접두어 · 억양 · 무음 제거)", open=False):
                with gr.Row():
                    custom_prefix_box = gr.Textbox(value="", placeholder="예: 오프닝", label="파일명 접두어")
                    temp_slider = gr.Slider(0.3, 1.0, value=0.6, step=0.05, label="억양 변화 (0.6 권장)")
                with gr.Row():
                    trim_chk = gr.Checkbox(value=True, label="앞뒤 무음 제거")
                    colab_url_box = gr.Textbox(value="", placeholder="자동 연결", label="코랩 수동 주소")

            with gr.Row(elem_classes="action-row"):
                gen_btn = gr.Button("음성 합성 시작", variant="primary", scale=4)
                stop_btn = gr.Button("중단", variant="stop", scale=1, elem_classes="main-stop-btn")

            status_box = gr.Textbox(
                value="대본 입력 후 '음성 합성 시작'을 누르세요.",
                show_label=False,
                container=False,
                lines=1,
                max_lines=1,
                interactive=False,
                elem_classes="status-toast",
            )

            gr.HTML('<div class="section-divider"></div>')
            gr.HTML('<div class="section-heading"><span>04 · 완성 오디오</span></div>')
            output_audio_html = gr.HTML(value=initial_output_html)

            with gr.Accordion("최근 생성 목록 (클릭 시 바로 재생)", open=False):
                history_table = gr.Dataframe(
                    headers=["파일명", "용량", "시각"],
                    datatype=["str", "str", "str"],
                    column_widths=["58%", "18%", "24%"],
                    value=initial_table,
                    show_label=False,
                    interactive=False,
                    wrap=False,
                )

    voice_dropdown.change(
        fn=on_voice_select,
        inputs=[voice_dropdown],
        outputs=[sample_audio_html, ref_text_box, sample_path_state],
        queue=False,
    )
    ref_text_box.blur(
        fn=save_ref_text_on_edit,
        inputs=[voice_dropdown, ref_text_box],
        outputs=[status_box],
        queue=False,
    )
    new_upload.upload(
        fn=on_upload_new_file,
        inputs=[new_upload, new_name],
        outputs=[new_name],
        queue=False,
    )
    folder_preset_radio.change(
        fn=set_preset_folder,
        inputs=[folder_preset_radio],
        outputs=[output_dir_box, history_table, status_box],
        queue=False,
    )
    browse_folder_btn.click(
        fn=browse_custom_folder,
        inputs=[output_dir_box],
        outputs=[output_dir_box, folder_preset_radio, history_table, status_box],
        queue=False,
    )
    output_dir_box.submit(
        fn=apply_custom_folder,
        inputs=[output_dir_box],
        outputs=[output_dir_box, folder_preset_radio, history_table, status_box],
        queue=False,
    )
    refresh_btn.click(
        fn=refresh_all,
        inputs=[output_dir_box],
        outputs=[voice_dropdown, sample_audio_html, ref_text_box, sample_path_state, colab_url_box, colab_status_txt, history_table],
        queue=False,
    )
    open_sample_btn.click(
        fn=open_sample_folder,
        inputs=[sample_path_state],
        outputs=[status_box],
        queue=False,
    )
    open_output_btn.click(
        fn=open_selected_output_folder,
        inputs=[output_dir_box, output_path_state],
        outputs=[status_box],
        queue=False,
    )
    colab_btn.click(fn=auto_start_colab_from_pc, inputs=[], outputs=[colab_status_txt, status_box], queue=False)
    colab_kill_btn.click(fn=shutdown_remote_colab, inputs=[colab_url_box], outputs=[colab_status_txt, status_box], queue=False)
    save_sample_btn.click(
        fn=save_new_sample,
        inputs=[new_upload, new_name, ref_text_box],
        outputs=[voice_dropdown, sample_audio_html, ref_text_box, sample_path_state, status_box],
        queue=False,
    )
    speed_slider.release(
        fn=apply_live_postprocess,
        inputs=[speed_slider, trim_chk, output_path_state, output_dir_box],
        outputs=[output_audio_html, history_table, status_box],
        queue=False,
    )
    trim_chk.change(
        fn=apply_live_postprocess,
        inputs=[speed_slider, trim_chk, output_path_state, output_dir_box],
        outputs=[output_audio_html, history_table, status_box],
        queue=False,
    )

    gen_event = gen_btn.click(
        fn=generate_cloned_voice,
        inputs=[
            voice_dropdown,
            ref_text_box,
            target_text_box,
            custom_prefix_box,
            speed_slider,
            trim_chk,
            temp_slider,
            engine_radio,
            colab_url_box,
            output_dir_box,
        ],
        outputs=[output_audio_html, output_path_state, status_box, history_table],
        api_name="generate_cloned_voice",
    )
    stop_btn.click(fn=request_cancel, inputs=[], outputs=[status_box], cancels=[gen_event], queue=False)

    history_table.select(
        fn=on_history_select,
        inputs=[history_table, output_dir_box],
        outputs=[output_audio_html, output_path_state, status_box],
        queue=False,
    )

    status_timer = gr.Timer(6.0)
    status_timer.tick(
        fn=lambda: get_status_label(force_refresh=False),
        inputs=[],
        outputs=[colab_status_txt],
        queue=False,
    )
    demo.load(
        fn=lambda: get_status_label(force_refresh=True),
        inputs=[],
        outputs=[colab_status_txt],
        queue=False,
    )

if __name__ == "__main__":
    threading.Thread(target=_prewarm_engine_background, daemon=True).start()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7865,
        inbrowser=False,
        css=STUDIO_CSS,
        js=FORCE_LIGHT_JS,
        theme=gr.themes.Base(),
        allowed_paths=[
            str(DESKTOP_OUT_DIR),
            str(DOWNLOADS_OUT_DIR),
            str(G_DRIVE_BASE),
            str(STUDIO_DIR),
            "C:\\",
            "G:\\",
        ],
    )
