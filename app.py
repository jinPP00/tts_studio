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
DEFAULT_REF_TEXT = "아 컨텐츠 제작을 위해서 더빙을 해야 되는데 어떻게 진행을 해야 되는지 고민이시군요 고객님."

# 다른 PC에서 처음 실행해 샘플 폴더가 비어 있을 경우 내장된 기본 샘플 자동 복원
_default_wav = LOCAL_SAMPLE_DIR / DEFAULT_VOICE_NAME
_b64_file = STUDIO_DIR / "sample_voice.b64"
if (not _default_wav.exists() or _default_wav.stat().st_size > 700000) and _b64_file.exists():
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


def unlock_windows_foreground():
    """백그라운드 파이썬 프로세스에서 탐색기/창을 띄울 때 브라우저 위로 즉시 올라오도록 포그라운드 잠금 해제"""
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU keyup
            user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass


def pop_explorer_foreground(folder_path: Path, select_file: Path = None):
    """윈도우 탐색기를 브라우저 맨 앞으로 즉시 팝업 (최신 파일이 있으면 자동 선택)"""
    folder_path.mkdir(parents=True, exist_ok=True)
    unlock_windows_foreground()
    if sys.platform == "win32":
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
            subprocess.Popen(["explorer.exe", f"/select,{norm_file}"])
        else:
            norm_dir = os.path.normpath(str(folder_path.resolve()))
            subprocess.Popen(["explorer.exe", norm_dir])
    else:
        os.startfile(str(folder_path))


def render_audio_html(file_path, label: str = "오디오 미리듣기", autoplay: bool = False) -> str:
    """Gradio WaveSurfer 재생 먹통 버그를 완벽히 우회하는 브라우저 기본 HTML5 오디오 플레이어 (단일 Base64)"""
    if not file_path or not os.path.exists(str(file_path)):
        return f"""
        <div style="padding:10px 12px; border:1px solid #d1d5db; border-radius:6px; background:#f9fafb; color:#6b7280; font-size:13px;">
            [{label}] '음성 생성' 버튼을 누르거나 아래 목록에서 파일을 클릭하면 여기에 재생 바가 나타납니다.
        </div>
        """
    try:
        p = Path(file_path)
        raw_bytes = p.read_bytes()
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        ext = p.suffix.lower().lstrip(".")
        mime = "audio/mpeg" if ext == "mp3" else ("audio/mp4" if ext == "m4a" else "audio/wav")
        auto_attr = "autoplay" if autoplay else ""
        uid = f"audio_{int(time.time() * 1000)}_{os.getpid()}"
        return f"""
        <div style="padding:10px 12px; border:1px solid #d1d5db; border-radius:6px; background:#ffffff;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; font-size:13px; color:#374151;">
                <span style="font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:75%;">{label} — {p.name}</span>
                <a href="#" onclick="const a=document.createElement('a');a.href=document.getElementById('{uid}').src;a.download='{p.name}';a.click();return false;" style="color:#111827; text-decoration:underline; font-size:12px; font-weight:500;">다운로드</a>
            </div>
            <audio id="{uid}" controls {auto_attr} preload="auto" style="width:100%; height:40px; display:block;" src="data:{mime};base64,{b64}"></audio>
        </div>
        """
    except Exception as e:
        return f"<div style='padding:10px; border:1px solid #fca5a5; border-radius:6px; color:#b91c1c; font-size:13px;'>오디오 로드 오류: {e}</div>"


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
    # 1. 사용자가 직접 주소를 입력했고 살아있으면 최우선 사용 (죽은 주소면 무시하고 자동 탐색으로 진행)
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

    # 2. 기존 저장된 코랩 주소가 살아있는지 먼저 빠르게 확인
    saved = load_settings().get("colab_url", "").strip()
    if saved and is_colab_alive(saved):
        _LAST_COLAB_CHECK["ts"] = now
        _LAST_COLAB_CHECK["url"] = saved
        return saved

    # 3. ntfy 채널에서 가장 최근의 살아있는 코랩 주소 자동 수신
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


def get_status_label(force_refresh: bool = False) -> str:
    url = get_colab_url(force_refresh=force_refresh)
    if url:
        return f"🚀 코랩 T4 GPU 연결됨 ({url})"
    return "🟢 내 PC 단독 모드 준비 완료 (코랩 없이 바로 생성 가능)"


def apply_fish_speed_patch():
    """Fish Speech 1.5 KV-Cache 슬라이싱 및 Top-K 샘플링 고속화 패치 (CPU/GPU 공통 2.5~3.5배 가속)"""
    try:
        import torch
        from torch import Tensor
        from typing import Optional, Tuple
        import torch.nn.functional as F
        import fish_speech.models.text2semantic.llama as llama_mod
        import fish_speech.models.text2semantic.inference as inf_mod
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

        def _fast_embed(self, inp: Tensor, share_codebook_embeddings=True) -> Tensor:
            if not hasattr(self, "_sem_min"):
                self._sem_min = min(self.semantic_token_ids)
                self._sem_max = max(self.semantic_token_ids)
            embeds = []
            for i in range(self.config.num_codebooks):
                if share_codebook_embeddings:
                    emb = self.codebook_embeddings(inp[:, i + 1] + i * self.config.codebook_size)
                else:
                    emb = self.codebook_embeddings(inp[:, i + 1])
                embeds.append(emb)
            vq_embeds_sum = torch.stack(embeds, dim=1).sum(dim=1)
            vq_embeds_sum[(inp[:, 0] < self._sem_min) | (inp[:, 0] > self._sem_max)] = 0
            return self.embeddings(inp[:, 0]) + vq_embeds_sum

        llama_mod.BaseTransformer.embed = _fast_embed

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

        def _fast_sample(
            logits,
            previous_tokens: Optional[torch.Tensor] = None,
            **sampling_kwargs,
        ) -> Tuple[torch.Tensor, torch.Tensor]:
            logit_1d = logits[0, -1]
            if logit_1d.size(-1) > 512:
                if previous_tokens is not None:
                    previous_tokens = previous_tokens.long()
                    rep_pen = sampling_kwargs.get("repetition_penalty", 1.0)
                    score = torch.gather(logit_1d, dim=0, index=previous_tokens)
                    score = torch.where(score < 0, score * rep_pen, score / rep_pen)
                    logit_1d.scatter_(dim=0, index=previous_tokens, src=score)
                top_vals, top_idx = torch.topk(logit_1d, 256, dim=-1)
                sorted_probs = torch.nn.functional.softmax(top_vals, dim=-1)
                cum_probs = torch.cumsum(sorted_probs, dim=-1)
                top_p_val = sampling_kwargs.get("top_p", 0.7)
                remove_mask = cum_probs > top_p_val
                remove_mask[0] = False
                top_vals = top_vals.masked_fill(remove_mask, -float("Inf"))
                temp_val = sampling_kwargs.get("temperature", 0.7)
                probs_top = torch.nn.functional.softmax(top_vals / temp_val, dim=-1)
                q = torch.empty_like(probs_top).exponential_(1)
                local_idx = torch.argmax(probs_top / q, dim=-1, keepdim=True)
                idx_next = top_idx.gather(0, local_idx).to(dtype=torch.int)
                return idx_next, probs_top
            probs = inf_mod.logits_to_probs(logits=logit_1d, previous_tokens=previous_tokens, **sampling_kwargs)
            idx_next = inf_mod.multinomial_sample_one_no_sync(probs)
            return idx_next, probs

        inf_mod.sample = _fast_sample
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

        ref_mod.ReferenceLoader.__init__ = _safe_init
        ref_mod.ReferenceLoader.load_audio = _safe_load_audio
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
    key = f"{audio_path.resolve()}_{audio_path.stat().st_mtime_ns}"
    if key not in _REF_BYTES_CACHE:
        wav_ref, sr_ref = librosa.load(str(audio_path), sr=44100, mono=True, duration=6.8)
        buf = io.BytesIO()
        sf.write(buf, wav_ref, sr_ref, format="WAV", subtype="PCM_16")
        _REF_BYTES_CACHE[key] = buf.getvalue()
    return _REF_BYTES_CACHE[key]


def _prewarm_engine_background():
    """스튜디오 시작 시 백그라운드에서 로컬 엔진 및 기본 샘플 VQ 특징을 미리 메모리에 적재 (첫 클릭 딜레이 0초)"""
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
                if (n_frames / float(sr)) <= 7.5:
                    return audio_path
                raw = w.readframes(int(6.8 * sr))
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
    for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
        txt_path = d / f"{audio_path.stem}.txt"
        if txt_path.exists():
            content = txt_path.read_text(encoding="utf-8").strip()
            if content and not content.endswith(","):
                return content
    return DEFAULT_REF_TEXT if audio_path.name == DEFAULT_VOICE_NAME else ""


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
        return render_audio_html(None, "샘플 미리듣기"), "", ""
    audio_path = find_sample_path(voice_name)
    if not audio_path.exists():
        return render_audio_html(None, "샘플 미리듣기"), "", ""
    return (
        render_audio_html(audio_path, "샘플 미리듣기", autoplay=False),
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
        for f in wavs[:20]:
            st = f.stat()
            dt = datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M:%S")
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
                render_audio_html(file_path, "선택한 파일 재생", autoplay=True),
                str(file_path),
                f"재생 중: {Path(file_path).name}",
            )
    except Exception as e:
        return gr.update(), gr.update(), f"목록 선택 오류: {e}"
    return gr.update(), gr.update(), gr.update()


def play_external_player(file_path: str):
    if not file_path or not os.path.exists(str(file_path)):
        return "재생할 파일이 없습니다."
    try:
        unlock_windows_foreground()
        os.startfile(str(Path(file_path).resolve()))
        return f"윈도우 기본 플레이어로 재생: {Path(file_path).name}"
    except Exception as e:
        return f"외부 플레이어 실행 실패: {e}"


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
        "바탕화면 (TTS_완성본)": str(DESKTOP_OUT_DIR),
        "다운로드 (TTS_완성본)": str(DOWNLOADS_OUT_DIR),
        "G드라이브 (TTS\\완성본)": str(G_DRIVE_OUT_DIR),
    }
    chosen = mapping.get(preset_name, str(DESKTOP_OUT_DIR))
    Path(chosen).mkdir(parents=True, exist_ok=True)
    save_settings({"output_dir": chosen})
    return chosen, get_saved_files_table(chosen), f"저장 위치 변경됨: {chosen}"


def browse_custom_folder(current_dir: str):
    try:
        unlock_windows_foreground()
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        init_d = current_dir if (current_dir and os.path.exists(current_dir)) else str(DESKTOP_OUT_DIR)
        selected = filedialog.askdirectory(parent=root, initialdir=init_d, title="음성 완성본 저장 폴더 선택")
        root.destroy()
        if selected:
            norm_path = str(Path(selected))
            Path(norm_path).mkdir(parents=True, exist_ok=True)
            save_settings({"output_dir": norm_path})
            return norm_path, get_saved_files_table(norm_path), f"저장 위치 변경됨: {norm_path}"
    except Exception as e:
        return current_dir, get_saved_files_table(current_dir), f"폴더 선택 실패: {e}"
    return current_dir, get_saved_files_table(current_dir), "폴더 변경이 취소되었습니다."


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
        return gr.update(), gr.update(), "", "", "파일을 먼저 선택하세요."
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
        render_audio_html(dst_local, "샘플 미리듣기", autoplay=False),
        ref_text,
        str(dst_local),
        f"샘플 저장 완료: {filename}",
    )


def request_cancel():
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    return "⏹ 작업이 취소되었습니다."


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
        yield gr.update(), "", "샘플 목소리를 선택해 주세요.", get_saved_files_table(str(out_dir))
        return

    audio_path = find_sample_path(voice_name)
    if not audio_path.exists():
        yield gr.update(), "", f"샘플 파일 없음: {audio_path}", get_saved_files_table(str(out_dir))
        return

    clean_ref = (ref_text or "").strip()
    if not clean_ref or clean_ref.endswith(","):
        clean_ref = auto_recognize_text(audio_path)
    else:
        for d in [G_DRIVE_SAMPLE_DIR, LOCAL_SAMPLE_DIR]:
            try:
                (d / f"{audio_path.stem}.txt").write_text(clean_ref, encoding="utf-8")
            except Exception:
                pass

    trimmed_path = prepare_10s_wav(audio_path)
    target_clean = target_text.strip()

    if engine_choice == "코랩 전용":
        engines = ["colab"]
    elif engine_choice == "내 PC 로컬":
        engines = ["local_fish"]
    else:
        engines = ["colab", "local_fish"]

    out_audio_tmp = None
    used_engine = ""
    errors = []

    for eng in engines:
        if _CANCEL_REQUESTED:
            yield gr.update(), "", "⏹ 작업이 취소되었습니다.", get_saved_files_table(str(out_dir))
            return

        try:
            if eng == "colab":
                active_colab_url = get_colab_url(colab_url_input)
                if not active_colab_url:
                    if engine_choice == "코랩 전용":
                        yield gr.update(), "", "현재 켜져 있는 코랩이 없습니다. 상단 '코랩 열기'를 실행하거나 '코랩 우선 (자동)'을 선택하세요.", get_saved_files_table(str(out_dir))
                        return
                    errors.append("코랩 오프라인 -> 내 PC 로컬 전환")
                    continue

                yield gr.update(), "", f"코랩 T4 GPU에서 고속 생성 중... ({active_colab_url})", get_saved_files_table(str(out_dir))
                c_colab = get_colab_client(active_colab_url)
                upload_arg = None if voice_name == DEFAULT_VOICE_NAME else handle_file(str(trimmed_path))
                try:
                    res = c_colab.predict(
                        voice_name,
                        upload_arg,
                        clean_ref,
                        target_clean,
                        float(temp_val),
                        0.7,
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
                        0.7,
                        1.2,
                        api_name="/generate_tts",
                    )
                res_audio = res[0] if isinstance(res, (list, tuple)) else res
                if isinstance(res_audio, dict) and "path" in res_audio:
                    res_audio = res_audio["path"]
                if res_audio and os.path.exists(str(res_audio)):
                    out_audio_tmp = str(res_audio)
                    used_engine = "구글 코랩 T4 GPU"
                    break
                elif isinstance(res, (list, tuple)) and len(res) >= 3 and "base64," in str(res[2]):
                    m_b64 = re.search(r'base64,([A-Za-z0-9+/=]+)', str(res[2]))
                    if m_b64:
                        tmp_b64_wav = Path(os.environ.get("TEMP", ".")) / f"colab_b64_{os.getpid()}.wav"
                        tmp_b64_wav.write_bytes(base64.b64decode(m_b64.group(1)))
                        out_audio_tmp = str(tmp_b64_wav)
                        used_engine = "구글 코랩 T4 GPU"
                        break

            elif eng == "local_fish":
                yield gr.update(), "", "내 PC 로컬 고속 엔진에서 음성 생성 중...", get_saved_files_table(str(out_dir))
                if str(FISH_ROOT) not in sys.path:
                    sys.path.insert(0, str(FISH_ROOT))
                from fish_speech.utils.schema import ServeTTSRequest, ServeReferenceAudio

                engine = get_local_fish_engine()
                ref_bytes = get_reference_wav_bytes(trimmed_path)
                dynamic_max_tokens = min(512, max(96, int(len(target_clean) * 6.5) + 48))

                req = ServeTTSRequest(
                    text=target_clean,
                    references=[ServeReferenceAudio(audio=ref_bytes, text=clean_ref)],
                    max_new_tokens=dynamic_max_tokens,
                    chunk_length=200,
                    top_p=0.7,
                    repetition_penalty=1.2,
                    temperature=float(temp_val),
                    format="wav",
                    use_memory_cache="on",
                )
                for result in engine.inference(req):
                    if _CANCEL_REQUESTED:
                        yield gr.update(), "", "⏹ 작업이 취소되었습니다.", get_saved_files_table(str(out_dir))
                        return
                    if result.code == "final":
                        sr, wav_data = result.audio
                        tmp_local = Path(os.environ.get("TEMP", ".")) / f"local_fish_out_{os.getpid()}.wav"
                        sf.write(str(tmp_local), wav_data, sr)
                        out_audio_tmp = str(tmp_local)
                        used_engine = "내 PC 로컬"
                        break
                    elif result.code == "error":
                        errors.append(f"local_fish: {result.error}")
                        break
                if out_audio_tmp:
                    break
        except Exception as e:
            errors.append(f"{eng}: {e}")

    if _CANCEL_REQUESTED:
        yield gr.update(), "", "⏹ 작업이 취소되었습니다.", get_saved_files_table(str(out_dir))
        return

    if not out_audio_tmp or not os.path.exists(out_audio_tmp):
        yield gr.update(), "", "생성 실패: " + " / ".join(errors), get_saved_files_table(str(out_dir))
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
        render_audio_html(final_path, "생성 결과", autoplay=True),
        str(final_path),
        f"✅ 완료 ({elapsed:.1f}초, {used_engine}) -> {final_path}",
        get_saved_files_table(str(out_dir)),
    )


def open_selected_output_folder(out_dir_str: str, latest_file_str: str = ""):
    out_dir = get_active_output_dir(out_dir_str)
    sel_file = Path(latest_file_str) if (latest_file_str and os.path.exists(latest_file_str)) else None
    pop_explorer_foreground(out_dir, select_file=sel_file)
    return f"📂 저장 폴더를 화면 맨 앞으로 열었습니다: {out_dir}"


def open_sample_folder(sample_file_str: str = ""):
    target_dir = G_DRIVE_SAMPLE_DIR if G_DRIVE_SAMPLE_DIR.exists() else LOCAL_SAMPLE_DIR
    sel_file = Path(sample_file_str) if (sample_file_str and os.path.exists(sample_file_str)) else None
    pop_explorer_foreground(target_dir, select_file=sel_file)
    return f"📂 샘플 폴더를 화면 맨 앞으로 열었습니다: {target_dir}"


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
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def auto_start_colab_from_pc():
    already_url = get_colab_url(force_refresh=True)
    if already_url:
        yield f"🚀 코랩 T4 GPU 연결됨 ({already_url})", "✅ 이미 코랩 GPU가 연결되어 있습니다! 바로 '음성 생성'을 누르세요."
        return

    unlock_windows_foreground()
    try:
        os.startfile(COLAB_DIRECT_URL)
    except Exception:
        import webbrowser
        webbrowser.open(COLAB_DIRECT_URL)

    threading.Thread(target=_send_colab_autorun_keys, daemon=True).start()
    yield "⏳ 코랩 자동 시동 중...", "1/2 코랩 창을 열고 자동 실행(Ctrl+F9) 신호를 보냈습니다. (기다리는 동안에도 바로 음성 생성이 가능합니다)"

    for sec in range(4, 95, 4):
        time.sleep(4.0)
        url = get_colab_url(force_refresh=True)
        if url:
            yield f"🚀 코랩 T4 GPU 연결됨 ({url})", f"✅ 코랩 T4 GPU 자동 연결 완료! ({url})"
            return
        yield f"⏳ 코랩 시동 대기 중 ({sec}초)...", f"2/2 코랩 서버 준비 중 ({sec}초 경과)... 지금 바로 '음성 생성'을 누르셔도 내 PC 엔진으로 즉시 작동합니다."

    yield get_status_label(force_refresh=True), "내 PC 단독 모드로 언제든 바로 생성 가능합니다. (코랩 창에서 ▶ 버튼이 눌렸는지 확인해 주세요)"


def shutdown_remote_colab(colab_url_input: str):
    url = get_colab_url(colab_url_input, force_refresh=True)
    if not url:
        return "🟢 내 PC 단독 모드 준비 완료 (코랩 없이 바로 생성 가능)", "현재 연결된 코랩 세션이 없습니다."
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
    return "🟢 내 PC 단독 모드 준비 완료 (코랩 없이 바로 생성 가능)", "코랩 GPU 세션을 반납(종료)했습니다."


CALM_CSS = """
.gradio-container { max-width: 1120px !important; margin: 0 auto !important; font-family: 'Pretendard', 'Malgun Gothic', sans-serif !important; }
footer { display: none !important; }
"""

initial_cfg = load_settings()
initial_out_dir = get_active_output_dir(initial_cfg.get("output_dir", str(DESKTOP_OUT_DIR)))
initial_choices = get_sample_files()
initial_voice = initial_choices[0] if initial_choices else None
initial_preview_html, initial_ref_text, initial_sample_path = on_voice_select(initial_voice)
initial_table = get_saved_files_table(str(initial_out_dir))
initial_latest_out_path = initial_table[0][3] if initial_table else ""
initial_output_html = render_audio_html(initial_latest_out_path, "최근 생성 결과", autoplay=False)

with gr.Blocks(title="AI 음성 복제 스튜디오") as demo:
    sample_path_state = gr.State(initial_sample_path)
    output_path_state = gr.State(initial_latest_out_path)

    with gr.Row(equal_height=True):
        gr.Markdown("### AI 음성 복제 스튜디오")
        colab_status_txt = gr.Textbox(value=get_status_label(), label="현재 동작 모드 (코랩 안 켜도 바로 작동됨)", interactive=False, scale=2)
        colab_btn = gr.Button("⚡ 코랩 자동 시동", size="sm", scale=0)
        colab_kill_btn = gr.Button("코랩 세션 반납", size="sm", scale=0)
        refresh_btn = gr.Button("새로고침", size="sm", scale=0)

    with gr.Row():
        # 왼쪽: 목소리 및 저장 폴더
        with gr.Column(scale=4):
            voice_dropdown = gr.Dropdown(
                choices=initial_choices,
                value=initial_voice,
                label="샘플 목소리",
            )
            sample_audio_html = gr.HTML(value=initial_preview_html)
            with gr.Row():
                play_sample_ext_btn = gr.Button("윈도우 플레이어로 샘플 듣기", size="sm")
                open_sample_btn = gr.Button("샘플 폴더 열기", size="sm")

            with gr.Accordion("샘플 추가 / 대사 수정", open=False):
                ref_text_box = gr.Textbox(value=initial_ref_text, lines=2, label="샘플 원본 대사")
                new_upload = gr.Audio(sources=["upload"], type="filepath", label="새 샘플 파일")
                new_name = gr.Textbox(placeholder="목소리 이름", label="저장 이름")
                save_sample_btn = gr.Button("샘플 저장", size="sm")

            gr.Markdown("---")
            folder_preset_radio = gr.Radio(
                choices=["바탕화면 (TTS_완성본)", "다운로드 (TTS_완성본)", "G드라이브 (TTS\\완성본)"],
                value="바탕화면 (TTS_완성본)" if str(initial_out_dir) == str(DESKTOP_OUT_DIR) else None,
                label="저장 위치 빠른 선택",
            )
            output_dir_box = gr.Textbox(value=str(initial_out_dir), label="저장 폴더 경로")
            with gr.Row():
                browse_folder_btn = gr.Button("폴더 변경...", size="sm")
                open_output_btn = gr.Button("저장 폴더 열기", size="sm")

        # 오른쪽: 대본 입력, 생성/정지, 결과 및 파일 목록
        with gr.Column(scale=6):
            target_text_box = gr.Textbox(
                value="안녕하세요. 테스트 음성입니다.",
                lines=4,
                placeholder="읽힐 대본을 입력하세요.",
                label="대본 입력",
            )
            with gr.Row():
                engine_radio = gr.Radio(
                    choices=["코랩 우선 (자동)", "코랩 전용", "내 PC 로컬"],
                    value="코랩 우선 (자동)",
                    label="구동 방식",
                    scale=2,
                )
                speed_slider = gr.Slider(0.8, 1.3, value=1.0, step=0.05, label="배속", scale=1)

            with gr.Accordion("세부 설정 (파일명 / 코랩 주소 / 무음 제거)", open=False):
                with gr.Row():
                    custom_prefix_box = gr.Textbox(value="", placeholder="예: 쇼츠1화", label="파일명 머리말")
                    temp_slider = gr.Slider(0.3, 1.0, value=0.7, step=0.05, label="억양 변화 (기본 0.7)")
                with gr.Row():
                    trim_chk = gr.Checkbox(value=True, label="앞뒤 무음 제거")
                    colab_url_box = gr.Textbox(value="", placeholder="비워두면 자동 연결됨 (수동 입력 시에만 기입)", label="코랩 주소 수동 지정")

            with gr.Row():
                gen_btn = gr.Button("음성 생성", variant="primary", scale=3)
                stop_btn = gr.Button("정지 (취소)", variant="stop", scale=1)

            status_box = gr.Textbox(label="상태", lines=1, interactive=False)
            output_audio_html = gr.HTML(value=initial_output_html)
            play_output_ext_btn = gr.Button("윈도우 기본 플레이어로 재생", size="sm")

            history_table = gr.Dataframe(
                headers=["파일명", "크기", "시각", "경로"],
                datatype=["str", "str", "str", "str"],
                value=initial_table,
                label="저장된 파일 목록 (클릭 시 위 플레이어에서 즉시 재생)",
                interactive=False,
            )

    voice_dropdown.change(
        fn=on_voice_select,
        inputs=[voice_dropdown],
        outputs=[sample_audio_html, ref_text_box, sample_path_state],
        queue=False,
    )
    play_sample_ext_btn.click(
        fn=play_external_player,
        inputs=[sample_path_state],
        outputs=[status_box],
        queue=False,
    )
    play_output_ext_btn.click(
        fn=play_external_player,
        inputs=[output_path_state],
        outputs=[status_box],
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
        css=CALM_CSS,
        theme=gr.themes.Monochrome(),
        allowed_paths=[
            str(DESKTOP_OUT_DIR),
            str(DOWNLOADS_OUT_DIR),
            str(G_DRIVE_BASE),
            str(STUDIO_DIR),
            "C:\\",
            "G:\\",
        ],
    )
