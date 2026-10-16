import os
import sys
import io
import re
import json
import time
import wave
import base64
import shutil
import ctypes
import threading
import subprocess
import unicodedata
import urllib.request
from pathlib import Path
from datetime import datetime

os.environ["EINX_FILTER_TRACEBACK"] = "false"

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

for _d in [DESKTOP_OUT_DIR, LOCAL_SAMPLE_DIR, G_DRIVE_SAMPLE_DIR, G_DRIVE_OUT_DIR]:
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

_default_wav = LOCAL_SAMPLE_DIR / DEFAULT_VOICE_NAME
_b64_file = STUDIO_DIR / "sample_voice.b64"
if (not _default_wav.exists() or _default_wav.stat().st_size < 900000 or _default_wav.stat().st_size > 1200000) and _b64_file.exists():
    try:
        _default_wav.write_bytes(base64.b64decode(_b64_file.read_bytes()))
        (LOCAL_SAMPLE_DIR / "recording-2026.-10.-16.-오후-1-19-43.txt").write_text(
            DEFAULT_REF_TEXT,
            encoding="utf-8",
        )
    except Exception:
        pass

NTFY_TOPIC = "jinpp00_fish_tts_colab_2026"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
_LOCAL_FISH_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_CANCEL_REQUESTED = False
_COLAB_CLIENT_CACHE = {}
_LAST_COLAB_CHECK = {"ts": 0.0, "url": ""}
_REF_BYTES_CACHE = {}


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
    """샌드박스(exebox) 데스크톱에서 실행 중이더라도 실제 사용자 화면(WinSta0\\Default)으로 스레드 전환"""
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
    """프로세스를 숨겨진 샌드박스 데스크톱이 아닌 실제 사용자 화면(WinSta0\\Default)에 직접 실행"""
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
        ctypes.windll.kernel32.CreateProcessW(
            None, buf, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)
        )
    except Exception:
        subprocess.Popen(cmd_line, shell=True)


def unlock_windows_foreground():
    """백그라운드 파이썬 프로세스에서 탐색기/창을 띄울 때 브라우저 위로 즉시 올라오도록 포그라운드 잠금 해제"""
    if sys.platform == "win32":
        try:
            _switch_thread_to_user_desktop()
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 2, 0)
            user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass


def pop_explorer_foreground(folder_path: Path, select_file: Path = None):
    """윈도우 탐색기를 실제 사용자 화면(WinSta0\\Default)의 브라우저 맨 앞으로 즉시 팝업"""
    folder_path.mkdir(parents=True, exist_ok=True)
    unlock_windows_foreground()
    if sys.platform == "win32":
        norm_dir = os.path.normpath(str(folder_path.resolve()))
        if select_file is None:
            wavs = sorted(
                [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS],
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
            f"Start-Sleep -Milliseconds 350; "
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
    else:
        os.startfile(str(folder_path))


def render_audio_html(file_path, label: str = "오디오 미리듣기", autoplay: bool = False) -> str:
    """세련된 스튜디오 스타일 HTML5 오디오 플레이어 카드 (단일 Base64)"""
    if not file_path or not os.path.exists(str(file_path)):
        return f"""
        <div class="audio-empty-card">
            <div class="audio-empty-dot"></div>
            <span>{label} · 대본을 입력하고 생성하거나 아래 목록에서 파일을 선택하세요</span>
        </div>
        """
    try:
        p = Path(file_path)
        raw_bytes = p.read_bytes()
        size_kb = len(raw_bytes) // 1024
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        ext = p.suffix.lower().lstrip(".")
        mime = "audio/mpeg" if ext == "mp3" else ("audio/mp4" if ext == "m4a" else "audio/wav")
        auto_attr = "autoplay" if autoplay else ""
        uid = f"audio_{int(time.time() * 1000)}_{os.getpid()}"
        return f"""
        <div class="audio-player-card">
            <div class="audio-card-header">
                <div class="audio-title-wrap">
                    <span class="audio-badge">{label}</span>
                    <span class="audio-filename" title="{p.name}">{p.name}</span>
                </div>
                <div class="audio-meta-wrap">
                    <span class="audio-size">{size_kb} KB</span>
                    <a href="#" class="audio-dl-btn" onclick="const a=document.createElement('a');a.href=document.getElementById('{uid}').src;a.download='{p.name}';a.click();return false;">저장</a>
                </div>
            </div>
            <audio id="{uid}" controls {auto_attr} preload="auto" class="studio-audio-el" src="data:{mime};base64,{b64}"></audio>
        </div>
        """
    except Exception as e:
        return f"<div class='audio-empty-card' style='color:#dc2626;'>오디오 로드 실패: {e}</div>"


def load_settings() -> dict:
    default_cfg = {
        "output_dir": str(DESKTOP_OUT_DIR),
        "also_save_gdrive": True,
        "colab_url": "",
    }
    if SETTINGS_FILE.exists():
        try:
            default_cfg.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return default_cfg


def save_settings(cfg: dict):
    try:
        cur = load_settings()
        cur.update(cfg)
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
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


def is_colab_alive(url: str) -> bool:
    if not url or not url.strip().startswith("http"):
        return False
    try:
        req = urllib.request.Request(f"{url.strip().rstrip('/')}/config", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=1.2) as r:
            return r.status == 200
    except Exception:
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
    if not force_refresh and (now - _LAST_COLAB_CHECK["ts"] < 4.0):
        return _LAST_COLAB_CHECK["url"]

    saved = load_settings().get("colab_url", "").strip()
    if saved and is_colab_alive(saved):
        _LAST_COLAB_CHECK["ts"] = now
        _LAST_COLAB_CHECK["url"] = saved
        return saved

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=all",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            lines = [json.loads(line) for line in resp.read().decode("utf-8").splitlines() if line.strip()]
            msgs = [m.get("message", "").strip() for m in lines if m.get("event") == "message"]
            for msg in reversed(msgs):
                if msg == "RESET_OLD_COLAB":
                    break
                if msg.startswith("https://") and "gradio.live" in msg and "33eb403275e9404ad6" not in msg and "test-colab" not in msg:
                    if is_colab_alive(msg):
                        save_settings({"colab_url": msg})
                        _LAST_COLAB_CHECK["ts"] = now
                        _LAST_COLAB_CHECK["url"] = msg
                        return msg
                    break
    except Exception:
        pass

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
    border_color = "rgba(16, 185, 129, 0.25)" if is_online else "rgba(15, 23, 42, 0.08)"
    text_color = "#065F46" if is_online else "#27272A"
    return f"""
    <div style="display:flex;align-items:center;justify-content:center;gap:8px;background:{bg_color};border:1px solid {border_color};border-radius:999px;padding:8px 16px;font-size:13px;font-weight:600;color:{text_color};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
        <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0;display:inline-block;"></span>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{text}</span>
    </div>
    """


def get_status_label(force_refresh: bool = False) -> str:
    url = get_colab_url(force_refresh=force_refresh)
    if url:
        short_host = url.replace("https://", "").replace("http://", "").split("/")[0]
        return render_status_badge(f"코랩 T4 GPU 가속 연결됨 · {short_host}", is_online=True)
    return render_status_badge("내 PC 단독 엔진 준비 완료 (코랩 없이 바로 생성)", is_online=False)


def apply_fish_speed_patch():
    """Fish Speech 1.5 KV-Cache 슬라이싱 패치 (음성 품질/샘플링 확률 분포는 원본 100% 유지)"""
    try:
        import torch
        from torch import Tensor
        from typing import Optional
        import torch.nn.functional as F
        import fish_speech.models.text2semantic.llama as llama_mod
        from contextlib import nullcontext
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
        for _rel in ["fish_speech/models/text2semantic/inference.py", "fish_speech/models/text2semantic/llama.py"]:
            _p = FISH_ROOT / _rel
            if _p.exists():
                _p.write_text(_p.read_text(encoding="utf-8").replace("from transformers import AutoTokenizer", "AutoTokenizer = None"), encoding="utf-8")

    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    hf_base = "https://huggingface.co/fishaudio/fish-speech-1.5/resolve/main"
    ms_base = "https://www.modelscope.cn/models/fishaudio/fish-speech-1.5/resolve/master"
    for fname in ["config.json", "special_tokens.json", "tokenizer.tiktoken", "firefly-gan-vq-fsq-8x1024-21hz-generator.pth", "model.pth"]:
        fpath = CKPT_DIR / fname
        if not fpath.exists() or fpath.stat().st_size < 500:
            for base in [hf_base, ms_base]:
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
        if not model_file.exists() or model_file.stat().st_size < 1200000000:
            ensure_local_fish_installed()

        if str(FISH_ROOT) not in sys.path:
            sys.path.insert(0, str(FISH_ROOT))

        import hashlib
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
                if use_cache == "on" and key in self.ref_by_hash:
                    tok, txt = self.ref_by_hash[key]
                else:
                    tok = self.encode_reference(reference_audio=ref.audio, enable_reference_audio=True)
                    txt = ref.text
                    self.ref_by_hash[key] = (tok, txt)
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


def _prewarm_engine_background():
    """스튜디오 시작 시 백그라운드에서 로컬 엔진 및 기본 샘플 VQ 특징을 미리 메모리에 적재"""
    try:
        model_file = CKPT_DIR / "model.pth"
        if not model_file.exists():
            return
        eng = get_local_fish_engine()
        sample_p = find_sample_path(DEFAULT_VOICE_NAME)
        if sample_p.exists():
            from fish_speech.utils.schema import ServeReferenceAudio
            ref_bytes = get_reference_wav_bytes(sample_p)
            ref_txt = auto_recognize_text(sample_p)
            eng.load_by_hash([ServeReferenceAudio(audio=ref_bytes, text=ref_txt)], "on")
    except Exception:
        pass


def prepare_10s_wav(audio_path: Path) -> Path:
    if audio_path.suffix.lower() == ".wav":
        try:
            with wave.open(str(audio_path), "rb") as w:
                n_channels = w.getnchannels()
                sampwidth = w.getsampwidth()
                sr = w.getframerate()
                n_frames = w.getnframes()
                if (n_frames / float(sr)) <= 13.0:
                    return audio_path
                raw = w.readframes(int(12.5 * sr))
            trimmed_path = Path(os.environ.get("TEMP", ".")) / f"trimmed_{audio_path.stem}.wav"
            with wave.open(str(trimmed_path), "wb") as w_out:
                w_out.setnchannels(n_channels)
                w_out.setsampwidth(sampwidth)
                w_out.setframerate(sr)
                w_out.writeframes(raw)
            return trimmed_path
        except Exception:
            return audio_path
    return audio_path


def find_sample_path(voice_name: str) -> Path:
    for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
        p = d / voice_name
        if p.exists():
            return p
    return LOCAL_SAMPLE_DIR / voice_name


def auto_recognize_text(audio_path: Path) -> str:
    if audio_path.name == DEFAULT_VOICE_NAME:
        for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
            txt_path = d / f"{audio_path.stem}.txt"
            if txt_path.exists():
                content = txt_path.read_text(encoding="utf-8").strip()
                if content and content != OLD_SHORT_REF_TEXT and not content.endswith(","):
                    return content
        return DEFAULT_REF_TEXT
    for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
        txt_path = d / f"{audio_path.stem}.txt"
        if txt_path.exists():
            content = txt_path.read_text(encoding="utf-8").strip()
            if content and not content.endswith(","):
                return content
    return ""


def get_sample_files():
    seen = {}
    for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                    seen[f.name] = f
    files = sorted(seen.keys())
    return files if files else ["(샘플 파일 없음)"]


def on_voice_select(voice_name):
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


def get_saved_files_table(out_dir_str: str = ""):
    out_dir = get_active_output_dir(out_dir_str)
    rows = []
    if out_dir.exists():
        wavs = sorted(
            [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for f in wavs[:6]:
            st = f.stat()
            dt = datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            rows.append([f.name, f"{st.st_size // 1024} KB", dt, str(f)])
    return rows


def on_history_select(evt: gr.SelectData, table_data, current_out_dir: str = ""):
    try:
        row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else int(evt.index)
        file_path = ""
        if hasattr(table_data, "iloc"):
            row = table_data.iloc[row_idx]
            file_path = str(row.iloc[3] if hasattr(row, "iloc") else row[3])
        elif isinstance(table_data, dict) and "data" in table_data:
            file_path = str(table_data["data"][row_idx][3])
        elif isinstance(table_data, (list, tuple)) and len(table_data) > row_idx:
            file_path = str(table_data[row_idx][3])

        if (not file_path or not os.path.exists(file_path)) and current_out_dir:
            rows = get_saved_files_table(current_out_dir)
            if len(rows) > row_idx:
                file_path = rows[row_idx][3]

        if file_path and os.path.exists(file_path):
            return (
                render_audio_html(file_path, "재생 중", autoplay=True),
                str(file_path),
                f"선택 트랙 재생 · {Path(file_path).name}",
            )
    except Exception as e:
        return gr.update(), gr.update(), f"목록 선택 오류: {e}"
    return gr.update(), gr.update(), gr.update()


def postprocess_audio(audio_path: str, speed: float, trim_silence: bool) -> str:
    try:
        if abs(speed - 1.0) < 0.02 and not trim_silence:
            return audio_path
        wav, sr = librosa.load(audio_path, sr=None, mono=True)
        if trim_silence:
            wav, _ = librosa.effects.trim(wav, top_db=28)
        if abs(speed - 1.0) >= 0.02:
            wav = librosa.effects.time_stretch(wav, rate=float(speed))
        out_tmp = Path(os.environ.get("TEMP", ".")) / f"post_{int(time.time()*1000)}.wav"
        sf.write(str(out_tmp), wav, sr)
        return str(out_tmp)
    except Exception:
        return audio_path


def set_preset_folder(preset_name: str):
    mapping = {
        "바탕화면": str(DESKTOP_OUT_DIR),
        "다운로드": str(DOWNLOADS_OUT_DIR),
        "G드라이브": str(G_DRIVE_OUT_DIR),
    }
    chosen = mapping.get(preset_name, str(DESKTOP_OUT_DIR))
    Path(chosen).mkdir(parents=True, exist_ok=True)
    save_settings({"output_dir": chosen})
    return chosen, get_saved_files_table(chosen), f"저장 위치 변경 · {chosen}"


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
                    return norm_path, get_saved_files_table(norm_path), f"저장 위치 변경 · {norm_path}"
                break
    except Exception as e:
        return current_dir, get_saved_files_table(current_dir), f"폴더 변경 실패: {e}"
    return current_dir, get_saved_files_table(current_dir), "폴더 변경 취소됨"


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


def save_new_sample(uploaded_file, custom_name):
    if not uploaded_file:
        return gr.update(), gr.update(), "", "", "오디오 파일을 먼저 선택하세요."
    src = Path(uploaded_file)
    stem = re.sub(r'[\\/:*?"<>|]', "_", custom_name.strip()) if custom_name and custom_name.strip() else src.stem
    filename = f"{stem}{src.suffix.lower()}"
    dst_local = LOCAL_SAMPLE_DIR / filename
    shutil.copy2(src, dst_local)
    try:
        shutil.copy2(src, G_DRIVE_SAMPLE_DIR / filename)
    except Exception:
        pass
    ref_text = auto_recognize_text(dst_local)
    choices = get_sample_files()
    return (
        gr.update(choices=choices, value=filename),
        render_audio_html(dst_local, "샘플", autoplay=False),
        ref_text,
        str(dst_local),
        f"새 목소리 등록 완료 · {filename}",
    )


def request_cancel():
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    return "합성 중단됨"


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
        for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
            try:
                (d / f"{audio_path.stem}.txt").write_text(clean_ref, encoding="utf-8")
            except Exception:
                pass

    trimmed_path = prepare_10s_wav(audio_path)
    target_clean = target_text.strip()

    if engine_choice == "코랩 GPU":
        engines = ["colab"]
    elif engine_choice == "내 PC":
        engines = ["local_fish"]
    else:
        engines = ["colab", "local_fish"]

    out_audio_tmp = None
    used_engine = ""
    errors = []

    for eng in engines:
        if _CANCEL_REQUESTED:
            yield gr.update(), "", "합성 중단됨", get_saved_files_table(str(out_dir))
            return

        try:
            if eng == "colab":
                active_colab_url = get_colab_url(colab_url_input)
                if not active_colab_url:
                    if engine_choice == "코랩 GPU":
                        yield gr.update(), "", "연결된 코랩이 없습니다. '자동' 또는 '내 PC'를 선택하세요.", get_saved_files_table(str(out_dir))
                        return
                    errors.append("코랩 오프라인 -> 내 PC 전환")
                    continue

                yield gr.update(), "", "코랩 GPU에서 합성 중...", get_saved_files_table(str(out_dir))
                c_colab = get_colab_client(active_colab_url)
                upload_arg = handle_file(str(trimmed_path))
                try:
                    res = c_colab.predict(
                        voice_name,
                        upload_arg,
                        clean_ref,
                        target_clean,
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
                        target_clean,
                        float(temp_val),
                        0.6,
                        1.2,
                        api_name="/generate_tts",
                    )
                res_audio = res[0] if isinstance(res, (list, tuple)) else res
                if isinstance(res_audio, dict) and "path" in res_audio:
                    res_audio = res_audio["path"]
                if res_audio and os.path.exists(str(res_audio)):
                    out_audio_tmp = str(res_audio)
                    used_engine = "코랩 GPU"
                    break
                elif isinstance(res, (list, tuple)) and len(res) >= 3 and "base64," in str(res[2]):
                    m_b64 = re.search(r"base64,([A-Za-z0-9+/=]+)", str(res[2]))
                    if m_b64:
                        tmp_b64_wav = Path(os.environ.get("TEMP", ".")) / f"colab_b64_{os.getpid()}.wav"
                        tmp_b64_wav.write_bytes(base64.b64decode(m_b64.group(1)))
                        out_audio_tmp = str(tmp_b64_wav)
                        used_engine = "코랩 GPU"
                        break

            elif eng == "local_fish":
                yield gr.update(), "", "내 PC 엔진에서 합성 중...", get_saved_files_table(str(out_dir))
                if str(FISH_ROOT) not in sys.path:
                    sys.path.insert(0, str(FISH_ROOT))
                import torch
                from fish_speech.utils.schema import ServeTTSRequest, ServeReferenceAudio

                engine = get_local_fish_engine()
                ref_bytes = get_reference_wav_bytes(trimmed_path)
                dynamic_max_tokens = min(512, max(96, int(len(target_clean) * 6.5) + 48))

                torch.manual_seed(42)
                req = ServeTTSRequest(
                    text=target_clean,
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
                        yield gr.update(), "", "합성 중단됨", get_saved_files_table(str(out_dir))
                        return
                    if result.code == "final":
                        sr, wav_data = result.audio
                        tmp_local = Path(os.environ.get("TEMP", ".")) / f"local_fish_out_{os.getpid()}.wav"
                        sf.write(str(tmp_local), wav_data, sr)
                        out_audio_tmp = str(tmp_local)
                        used_engine = "내 PC"
                        break
                    elif result.code == "error":
                        errors.append(f"local_fish: {result.error}")
                        break
                if out_audio_tmp:
                    break
        except Exception as e:
            errors.append(f"{eng}: {e}")

    if _CANCEL_REQUESTED:
        yield gr.update(), "", "합성 중단됨", get_saved_files_table(str(out_dir))
        return

    if not out_audio_tmp or not os.path.exists(out_audio_tmp):
        yield gr.update(), "", "합성 실패 · " + " / ".join(errors), get_saved_files_table(str(out_dir))
        return

    processed_tmp = postprocess_audio(out_audio_tmp, float(speed_val), bool(trim_val))

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    preview_slug = re.sub(r"[^가-힣a-zA-Z0-9]", "", target_clean)[:12] or "tts"
    voice_slug = re.sub(r"[^가-힣a-zA-Z0-9_-]", "", audio_path.stem)[:14] or "voice"
    prefix_clean = re.sub(r'[\\/:*?"<>| ]', "_", (custom_prefix or "").strip())
    out_filename = f"{prefix_clean}_{now_str}_{preview_slug}.wav" if prefix_clean else f"{now_str}_{voice_slug}_{preview_slug}.wav"

    final_path = out_dir / out_filename
    shutil.copy2(processed_tmp, final_path)

    if load_settings().get("also_save_gdrive", True):
        try:
            if out_dir.resolve() != G_DRIVE_OUT_DIR.resolve():
                G_DRIVE_OUT_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(processed_tmp, G_DRIVE_OUT_DIR / out_filename)
        except Exception:
            pass

    elapsed = time.time() - t_start
    yield (
        render_audio_html(final_path, "완성", autoplay=True),
        str(final_path),
        f"합성 완료 ({elapsed:.1f}초 · {used_engine}) · {out_filename}",
        get_saved_files_table(str(out_dir)),
    )


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


COLAB_DIRECT_URL = "https://colab.research.google.com/github/jinPP00/tts_studio/blob/main/fish_tts_studio.ipynb"


def _send_colab_autorun_keys():
    """크롬에서 코랩 탭이 열리면 자동으로 Ctrl+F9(모두 실행) 및 경고창 확인(Tab->Enter) 입력"""
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
        return render_status_badge("내 PC 단독 엔진 준비 완료 (코랩 없이 바로 생성)", is_online=False), "현재 연결된 코랩 세션이 없습니다."
    try:
        c = get_colab_client(url)
        c.predict(api_name="/shutdown_colab")
    except Exception:
        pass
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=b"RESET_OLD_COLAB", method="POST"),
            timeout=3,
        )
    except Exception:
        pass
    save_settings({"colab_url": ""})
    _LAST_COLAB_CHECK["url"] = ""
    _LAST_COLAB_CHECK["ts"] = time.time()
    return render_status_badge("내 PC 단독 엔진 준비 완료 (코랩 없이 바로 생성)", is_online=False), "코랩 GPU 세션을 안전하게 반납(종료)했습니다."


FORCE_LIGHT_JS = """
() => {
    document.documentElement.classList.remove('dark');
    if (document.body) {
        document.body.classList.remove('dark');
        document.body.style.colorScheme = 'light';
    }
    document.documentElement.style.colorScheme = 'light';
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
    max-width: 1220px !important;
    margin: 0 auto !important;
    padding: 24px 24px 48px 24px !important;
}

footer { display: none !important; }

/* 모든 블록 라벨, 타이틀, 스팬, 입력 텍스트 가독성 강제 보장 */
label, .block label, [data-testid="block-info"], .label-wrap, .label-wrap span {
    color: #18181B !important;
    font-weight: 600 !important;
    font-size: 13.5px !important;
    white-space: nowrap !important;
}

input, textarea, select, .single-select, .wrap-inner, .secondary-wrap {
    color: #18181B !important;
    background-color: #F8F8F7 !important;
}

/* 드롭다운 메뉴 및 선택값 글자색 강제 지정 */
ul.options, ul.options li, .item {
    background-color: #FFFFFF !important;
    color: #18181B !important;
}
ul.options li:hover, ul.options li.selected {
    background-color: #EEF2FF !important;
    color: #1E3A8A !important;
    font-weight: 700 !important;
}

/* 라디오 버튼 & 체크박스: 검은 배경 절대 금지! 선택 시 밝은 블루(#EEF2FF) 배경 + 진한 글자(#1E3A8A)로 100% 가독성 보장 */
.wrap {
    gap: 8px !important;
    display: flex !important;
    flex-wrap: wrap !important;
}
label.svelte-1p9xokt, .gradio-radio label, .gradio-checkbox label, fieldset label {
    background: #F4F4F5 !important;
    color: #18181B !important;
    border: 1.5px solid #D4D4D8 !important;
    border-radius: 10px !important;
    padding: 7px 13px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    white-space: nowrap !important;
    word-break: keep-all !important;
    cursor: pointer !important;
    transition: all 0.12s ease !important;
}
label.svelte-1p9xokt span, .gradio-radio label span, .gradio-checkbox label span, fieldset label span {
    color: #18181B !important;
}
label.svelte-1p9xokt.selected,
.gradio-radio label.selected,
.gradio-radio label:has(input:checked),
fieldset label.selected,
fieldset label:has(input:checked) {
    background: #EEF2FF !important;
    color: #1E3A8A !important;
    border: 1.5px solid #2563EB !important;
    box-shadow: 0 1px 4px rgba(37, 99, 235, 0.12) !important;
}
label.svelte-1p9xokt.selected span,
.gradio-radio label.selected span,
.gradio-radio label:has(input:checked) span,
fieldset label.selected span,
fieldset label:has(input:checked) span {
    color: #1E3A8A !important;
    font-weight: 700 !important;
}
input[type="radio"], input[type="checkbox"] {
    accent-color: #2563EB !important;
}

/* 데이터프레임 / 테이블 검은배경에 검은글자 문제 완전 해결 */
table, thead, tbody, tr, th, td,
.table-wrap, .svelte-1b7421c, [data-testid="dataframe"], [data-testid="dataframe"] * {
    background-color: #FFFFFF !important;
    color: #18181B !important;
    border-color: #E4E4E7 !important;
    font-size: 13px !important;
}
thead th, thead tr th, .header-row th {
    background-color: #F4F4F5 !important;
    color: #18181B !important;
    font-weight: 700 !important;
    border-bottom: 1px solid #D4D4D8 !important;
}
tbody tr:nth-child(even) td {
    background-color: #FAFAFA !important;
    color: #18181B !important;
}
tbody tr:hover td {
    background-color: #EEF2FF !important;
    color: #1E3A8A !important;
    cursor: pointer !important;
}
.cell-wrap, .cell-wrap span, td span, th span {
    color: #18181B !important;
    background: transparent !important;
}

/* 상단 헤더 바 */
.studio-topbar {
    background: #FFFFFF !important;
    border: 1px solid #E4E4E7 !important;
    border-radius: 16px !important;
    padding: 12px 20px !important;
    margin-bottom: 20px !important;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.03) !important;
    align-items: center !important;
}

.brand-header {
    display: flex;
    align-items: center;
    gap: 10px;
    white-space: nowrap;
}
.brand-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #2563EB;
    box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
    flex-shrink: 0;
}
.brand-title {
    font-size: 17px;
    font-weight: 700;
    color: #18181B !important;
    letter-spacing: -0.025em;
    margin: 0;
}
.brand-sub {
    font-size: 12px;
    color: #52525B !important;
    font-weight: 600;
    margin-left: 6px;
}

/* 좌우 메인 카드 패널 */
.studio-card {
    background: #FFFFFF !important;
    border: 1px solid #E4E4E7 !important;
    border-radius: 18px !important;
    padding: 24px !important;
    box-shadow: 0 4px 20px -2px rgba(15, 23, 42, 0.04) !important;
    gap: 14px !important;
}

.section-heading {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #52525B !important;
    margin: 0 0 2px 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    white-space: nowrap;
}
.section-heading span {
    color: #52525B !important;
}

.section-divider {
    height: 1px;
    background: #E4E4E7;
    margin: 8px 0;
}

/* 커스텀 오디오 플레이어 카드 */
.audio-player-card {
    background: #F8F8F7;
    border: 1px solid #E4E4E7;
    border-radius: 14px;
    padding: 14px 16px;
}
.audio-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    gap: 8px;
}
.audio-title-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex: 1;
}
.audio-badge {
    background: #2563EB !important;
    color: #FFFFFF !important;
    font-size: 11px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 6px;
    white-space: nowrap;
}
.audio-filename {
    font-size: 13px;
    font-weight: 600;
    color: #18181B !important;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.audio-meta-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
}
.audio-size {
    font-size: 11.5px;
    color: #52525B !important;
    font-weight: 600;
}
.audio-dl-btn {
    background: #FFFFFF !important;
    border: 1px solid #D4D4D8 !important;
    color: #18181B !important;
    text-decoration: none !important;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 7px;
}
.audio-dl-btn:hover {
    background: #2563EB !important;
    border-color: #2563EB !important;
    color: #FFFFFF !important;
}
.studio-audio-el {
    width: 100%;
    height: 38px;
    display: block;
    border-radius: 8px;
}

.audio-empty-card {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 16px 18px;
    border: 1px dashed #D4D4D8;
    border-radius: 14px;
    background: #FAFAFA;
    color: #52525B !important;
    font-size: 13px;
    font-weight: 500;
}
.audio-empty-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #A1A1AA;
    flex-shrink: 0;
}

/* 대본 입력 에디터 */
.script-editor textarea {
    background: #F8F8F7 !important;
    border: 1px solid #D4D4D8 !important;
    border-radius: 14px !important;
    padding: 16px 18px !important;
    font-size: 15.5px !important;
    line-height: 1.65 !important;
    color: #18181B !important;
}
.script-editor textarea:focus {
    background: #FFFFFF !important;
    border-color: #2563EB !important;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1) !important;
}

/* 주요 실행 버튼: 짙은 네이비/블루(#1D4ED8) + 흰색 글자로 검은배경+검은글자 원천 차단 */
button.primary {
    background: #1D4ED8 !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 12px !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    min-height: 48px !important;
    white-space: nowrap !important;
    box-shadow: 0 4px 12px rgba(29, 78, 216, 0.2) !important;
}
button.primary, button.primary *, button.primary span {
    color: #FFFFFF !important;
}
button.primary:hover {
    background: #1E40AF !important;
}

button.secondary, button.stop {
    background: #F4F4F5 !important;
    color: #18181B !important;
    border: 1px solid #D4D4D8 !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    white-space: nowrap !important;
}
button.secondary *, button.stop * {
    color: #18181B !important;
}
button.secondary:hover, button.stop:hover {
    background: #E4E4E7 !important;
    color: #09090B !important;
}
.main-stop-btn {
    min-height: 48px !important;
    border-radius: 12px !important;
}

/* 상태 알림 바 */
.status-toast textarea, .status-toast input {
    background: #F4F4F5 !important;
    border: 1px solid #E4E4E7 !important;
    border-radius: 10px !important;
    padding: 10px 14px !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    color: #18181B !important;
}

/* 아코디언 정돈 */
.gradio-accordion {
    border: 1px solid #E4E4E7 !important;
    border-radius: 12px !important;
    background: #FAFAFA !important;
    overflow: hidden !important;
}
"""

initial_cfg = load_settings()
initial_out_dir = get_active_output_dir(initial_cfg.get("output_dir", str(DESKTOP_OUT_DIR)))
initial_choices = get_sample_files()
initial_voice = initial_choices[0] if initial_choices else None
initial_preview_html, initial_ref_text, initial_sample_path = on_voice_select(initial_voice)
initial_table = get_saved_files_table(str(initial_out_dir))
initial_latest_out_path = initial_table[0][3] if initial_table else ""
initial_output_html = render_audio_html(initial_latest_out_path, "완성본", autoplay=False)

with gr.Blocks(title="Voice Studio") as demo:
    sample_path_state = gr.State(initial_sample_path)
    output_path_state = gr.State(initial_latest_out_path)

    # 상단 헤더 바
    with gr.Row(elem_classes="studio-topbar", equal_height=True):
        with gr.Column(scale=3, min_width=190):
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
        with gr.Column(scale=5, min_width=280):
            colab_status_txt = gr.HTML(value=get_status_label())
        colab_btn = gr.Button("코랩 시동", variant="secondary", size="sm", scale=0)
        colab_kill_btn = gr.Button("코랩 종료", variant="secondary", size="sm", scale=0)
        refresh_btn = gr.Button("새로고침", variant="secondary", size="sm", scale=0)

    # 메인 2단 레이아웃 (한 화면에 들어오도록 컴팩트 구성)
    with gr.Row(equal_height=False):
        # 좌측: 목소리 & 저장 폴더
        with gr.Column(scale=4, elem_classes="studio-card"):
            gr.HTML('<div class="section-heading"><span>01 · 목소리</span></div>')
            voice_dropdown = gr.Dropdown(
                choices=initial_choices,
                value=initial_voice,
                label="목소리 선택",
                container=True,
            )
            sample_audio_html = gr.HTML(value=initial_preview_html)
            open_sample_btn = gr.Button("📂 샘플 폴더 열기", variant="secondary", size="sm")

            with gr.Accordion("새 목소리 추가 / 대사 수정", open=False):
                ref_text_box = gr.Textbox(
                    value=initial_ref_text,
                    lines=2,
                    label="샘플 대사",
                )
                new_upload = gr.Audio(sources=["upload"], type="filepath", label="오디오 파일 업로드")
                with gr.Row():
                    new_name = gr.Textbox(placeholder="이름 입력", label="저장 이름", scale=2)
                    save_sample_btn = gr.Button("저장", variant="secondary", scale=1)

            gr.HTML('<div class="section-divider"></div>')
            gr.HTML('<div class="section-heading"><span>02 · 저장 폴더</span></div>')
            folder_preset_radio = gr.Radio(
                choices=["바탕화면", "다운로드", "G드라이브"],
                value="바탕화면" if str(initial_out_dir) == str(DESKTOP_OUT_DIR) else None,
                label="저장 위치",
            )
            output_dir_box = gr.Textbox(value=str(initial_out_dir), label="폴더 경로")
            with gr.Row():
                browse_folder_btn = gr.Button("위치 변경", variant="secondary", size="sm")
                open_output_btn = gr.Button("📂 저장 폴더 열기", variant="secondary", size="sm")

        # 우측: 대본 입력 & 결과 오디오
        with gr.Column(scale=6, elem_classes="studio-card"):
            gr.HTML('<div class="section-heading"><span>03 · 음성 합성</span></div>')
            target_text_box = gr.Textbox(
                value="안녕하세요. 테스트 음성입니다.",
                lines=3,
                placeholder="읽힐 대본을 입력하세요.",
                label="대본 입력",
                elem_classes="script-editor",
            )

            with gr.Row(equal_height=True):
                engine_radio = gr.Radio(
                    choices=["자동 (코랩 우선)", "코랩 GPU", "내 PC"],
                    value="자동 (코랩 우선)",
                    label="엔진",
                    scale=3,
                    min_width=300,
                )
                speed_slider = gr.Slider(
                    0.8,
                    1.3,
                    value=1.0,
                    step=0.05,
                    label="배속",
                    scale=2,
                    min_width=170,
                )

            with gr.Accordion("상세 설정 (파일명 · 억양 · 무음)", open=False):
                with gr.Row():
                    custom_prefix_box = gr.Textbox(value="", placeholder="예: 오프닝", label="파일명 접두어")
                    temp_slider = gr.Slider(0.3, 1.0, value=0.6, step=0.05, label="억양 변화 (0.6 권장)")
                with gr.Row():
                    trim_chk = gr.Checkbox(value=True, label="앞뒤 무음 제거")
                    colab_url_box = gr.Textbox(value="", placeholder="자동 연결", label="코랩 주소")

            with gr.Row():
                gen_btn = gr.Button("음성 합성 시작", variant="primary", scale=4)
                stop_btn = gr.Button("중단", variant="stop", scale=1, elem_classes="main-stop-btn")

            status_box = gr.Textbox(
                value="대본 입력 후 '음성 합성 시작'을 누르세요.",
                show_label=False,
                container=False,
                lines=1,
                interactive=False,
                elem_classes="status-toast",
            )

            gr.HTML('<div class="section-divider"></div>')
            gr.HTML('<div class="section-heading"><span>04 · 완성 오디오</span></div>')
            output_audio_html = gr.HTML(value=initial_output_html)

            with gr.Accordion("최근 생성 목록 (클릭 시 재생)", open=False):
                history_table = gr.Dataframe(
                    headers=["파일명", "용량", "시각", "경로"],
                    datatype=["str", "str", "str", "str"],
                    value=initial_table,
                    show_label=False,
                    interactive=False,
                )

    voice_dropdown.change(
        fn=on_voice_select,
        inputs=[voice_dropdown],
        outputs=[sample_audio_html, ref_text_box, sample_path_state],
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
        outputs=[output_dir_box, history_table, status_box],
        queue=False,
    )
    output_dir_box.change(
        fn=lambda d: get_saved_files_table(d),
        inputs=[output_dir_box],
        outputs=[history_table],
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
        inputs=[new_upload, new_name],
        outputs=[voice_dropdown, sample_audio_html, ref_text_box, sample_path_state, status_box],
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
