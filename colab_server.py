import os
import sys
import io
import re
import time
import shutil
import subprocess
import base64
import threading
import urllib.request
from pathlib import Path
from datetime import datetime

os.environ["EINX_FILTER_TRACEBACK"] = "false"

SAMPLE_DIR = Path("/content/tts_data/샘플목소리")
OUTPUT_DIR = Path("/content/tts_data/완성본")
FISH_DIR = Path("/content/fish-speech-v15")
CKPT_DIR = FISH_DIR / "checkpoints" / "fish-speech-1.5"

SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE_NAME = "recording-2026.-10.-16.-오후-1-19-43.wav"
DEFAULT_REF_TEXT = (
    "아 컨텐츠 제작을 위해서 더빙을 해야 되는데 어떻게 진행을 해야 되는지 고민이시군요 고객님. "
    "그러실 때는 타입캐스트를 사용해 보시는 걸 추천드리는데요."
)
OLD_SHORT_REF_TEXT = "아 컨텐츠 제작을 위해서 더빙을 해야 되는데 어떻게 진행을 해야 되는지 고민이시군요 고객님."


def download_url_to_file(url: str, dst: Path, min_size: int = 100) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_suffix(dst.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_dst, "wb") as out_f:
            shutil.copyfileobj(resp, out_f, length=1024 * 1024)
        if tmp_dst.exists() and tmp_dst.stat().st_size >= min_size:
            tmp_dst.replace(dst)
            return True
    except Exception:
        pass
    try:
        subprocess.run(
            ["curl", "-L", "-s", "--retry", "3", "-A", "Mozilla/5.0", url, "-o", str(tmp_dst)],
            check=False,
        )
        if tmp_dst.exists() and tmp_dst.stat().st_size >= min_size:
            tmp_dst.replace(dst)
            return True
    except Exception:
        pass
    return False


def ensure_default_sample():
    target_wav = SAMPLE_DIR / DEFAULT_VOICE_NAME
    target_txt = SAMPLE_DIR / "recording-2026.-10.-16.-오후-1-19-43.txt"
    # 12.4초 완결 2문장 샘플(~1.09MB) 유지
    if not target_wav.exists() or target_wav.stat().st_size < 900000 or target_wav.stat().st_size > 1200000:
        for b64_url in [
            "https://raw.githubusercontent.com/jinPP00/tts_studio/main/sample_voice.b64",
            "https://gist.githubusercontent.com/jinPP00/7990cb24cd047b3f2622296b7d2ed4fa/raw/sample_voice.b64",
        ]:
            try:
                req = urllib.request.Request(b64_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    b64_bytes = r.read()
                target_wav.write_bytes(base64.b64decode(b64_bytes))
                target_txt.write_text(DEFAULT_REF_TEXT, encoding="utf-8")
                break
            except Exception:
                pass


ensure_default_sample()
print("1/4 기본 환경 준비 중...")

os.chdir("/content")
if not (FISH_DIR / ".project-root").exists():
    shutil.rmtree(FISH_DIR, ignore_errors=True)
    subprocess.run(
        ["git", "clone", "-b", "v1.5.1", "--depth", "1", "https://github.com/fishaudio/fish-speech.git", str(FISH_DIR)],
        check=False,
    )

FISH_DIR.mkdir(parents=True, exist_ok=True)
CKPT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(FISH_DIR))
if str(FISH_DIR) not in sys.path:
    sys.path.insert(0, str(FISH_DIR))

print("2/4 필수 패키지 설치 확인 중...")
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
        "tiktoken", "silero-vad", "gradio", "soundfile", "librosa", "einops", "python-dotenv",
    ],
    check=False,
)

print("3/4 음성 복제 모델 파일 확인 중...")
CHECKPOINT_SPECS = [
    ("config.json", 200),
    ("special_tokens.json", 1000),
    ("tokenizer.tiktoken", 100000),
    ("firefly-gan-vq-fsq-8x1024-21hz-generator.pth", 10000000),
    ("model.pth", 1000000000),
]

for fname, min_bytes in CHECKPOINT_SPECS:
    fpath = CKPT_DIR / fname
    if fpath.exists() and fpath.stat().st_size >= min_bytes:
        continue
    urls = [
        f"https://huggingface.co/fishaudio/fish-speech-1.5/resolve/main/{fname}",
        f"https://www.modelscope.cn/api/v1/models/fishaudio/fish-speech-1.5/repo?Revision=master&FilePath={fname}",
        f"https://www.modelscope.cn/models/fishaudio/fish-speech-1.5/resolve/master/{fname}",
    ]
    for u in urls:
        if download_url_to_file(u, fpath, min_size=min_bytes):
            break

(FISH_DIR / "fish_speech" / "utils").mkdir(parents=True, exist_ok=True)
(FISH_DIR / "fish_speech" / "utils" / "__init__.py").write_text(
    """import random
import numpy as np
import torch
from loguru import logger as _logger
from .braceexpand import braceexpand
from .context import autocast_exclude_mps
from .file import get_latest_checkpoint

def RankedLogger(*args, **kwargs):
    return _logger

def set_seed(seed: int):
    if seed < 0:
        seed = -seed
    if seed > (1 << 31):
        seed = 1 << 31
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
""",
    encoding="utf-8",
)

_file_py = FISH_DIR / "fish_speech" / "utils" / "file.py"
if _file_py.exists():
    _file_py.write_text(_file_py.read_text(encoding="utf-8").replace("from natsort import natsorted", "natsorted = sorted"), encoding="utf-8")

for _rel in ["fish_speech/models/text2semantic/inference.py", "fish_speech/models/text2semantic/llama.py"]:
    _p = FISH_DIR / _rel
    if _p.exists():
        _p.write_text(_p.read_text(encoding="utf-8").replace("from transformers import AutoTokenizer", "AutoTokenizer = None"), encoding="utf-8")

_vq_inf = FISH_DIR / "fish_speech" / "models" / "vqgan" / "inference.py"
if _vq_inf.exists():
    _vq_inf.write_text(
        _vq_inf.read_text(encoding="utf-8").replace(
            'OmegaConf.register_new_resolver("eval", eval)',
            'OmegaConf.register_new_resolver("eval", eval, replace=True)',
        ),
        encoding="utf-8",
    )

from omegaconf import OmegaConf
OmegaConf.register_new_resolver("eval", eval, replace=True)

if "tts_engine" not in globals():
    for _mod_name in list(sys.modules.keys()):
        if _mod_name.startswith("fish_speech"):
            del sys.modules[_mod_name]

import hashlib
import numpy as np
import torch
import librosa
import soundfile as sf
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


def apply_fish_speed_patch():
    """CPU 모드일 때만 KV-Cache 슬라이싱 적용 (음성 품질 및 샘플링 확률 분포는 원본 100% 보존)"""
    try:
        from torch import Tensor
        from typing import Optional
        import torch.nn.functional as F
        import fish_speech.models.text2semantic.llama as llama_mod

        if not torch.cuda.is_available():
            def _fast_kv_update_cpu(self, input_pos: Tensor, k_val: Tensor, v_val: Tensor):
                self.k_cache[:, :, input_pos] = k_val
                self.v_cache[:, :, input_pos] = v_val
                end_pos = int(input_pos[-1]) + 1
                return self.k_cache[:, :, :end_pos], self.v_cache[:, :, :end_pos]

            llama_mod.KVCache.update = _fast_kv_update_cpu

            def _fast_forward_generate_cpu(self, inp: Tensor, input_pos: Optional[Tensor] = None, return_all: bool = False):
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
                token_logits = F.linear(slow_out, self.embeddings.weight) if self.config.tie_word_embeddings else self.output(slow_out)
                return llama_mod.BaseTransformerForwardResult(logits=token_logits, hidden_states=x)

            llama_mod.BaseTransformer.forward_generate = _fast_forward_generate_cpu

            def _fast_forward_generate_fast_cpu(self, x: Tensor, input_pos: Optional[Tensor] = None) -> Tensor:
                x = x.view(1, 1, -1)
                end_pos = int(input_pos[-1]) + 1
                fast_mask = self.causal_mask[None, None, input_pos, :end_pos]
                fast_freqs_cis = self.fast_freqs_cis[input_pos]
                for layer in self.fast_layers:
                    x = layer(x, fast_freqs_cis, fast_mask, input_pos=input_pos)
                return self.fast_output(self.fast_norm(x))

            llama_mod.DualARTransformer.forward_generate_fast = _fast_forward_generate_fast_cpu
    except Exception:
        pass


apply_fish_speed_patch()

from fish_speech.inference_engine import TTSInferenceEngine
from fish_speech.models.text2semantic.inference import launch_thread_safe_queue
from fish_speech.models.vqgan.inference import load_model as load_decoder_model
from fish_speech.utils.schema import ServeTTSRequest, ServeReferenceAudio

device = "cuda" if torch.cuda.is_available() else "cpu"
precision = torch.half if device == "cuda" else torch.float32

print(f"4/4 엔진 시작 중 ({'T4 GPU 고속 가속' if device == 'cuda' else 'CPU 고속 모드'})...")
if "tts_engine" not in globals():
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
    tts_engine = TTSInferenceEngine(
        llama_queue=llama_queue,
        decoder_model=decoder_model,
        compile=False,
        precision=precision,
    )

_COLAB_REF_CACHE = {}


def get_colab_ref_bytes(audio_path: Path) -> bytes:
    key = f"{audio_path.resolve()}_{audio_path.stat().st_mtime_ns}_13s"
    if key not in _COLAB_REF_CACHE:
        wav_ref, sr_ref = librosa.load(str(audio_path), sr=44100, mono=True, duration=13.0)
        buf = io.BytesIO()
        sf.write(buf, wav_ref, sr_ref, format="WAV", subtype="PCM_16")
        _COLAB_REF_CACHE[key] = buf.getvalue()
    return _COLAB_REF_CACHE[key]


try:
    _def_p = SAMPLE_DIR / DEFAULT_VOICE_NAME
    if _def_p.exists():
        _b = get_colab_ref_bytes(_def_p)
        tts_engine.load_by_hash([ServeReferenceAudio(audio=_b, text=DEFAULT_REF_TEXT)], "on")
except Exception:
    pass

import gradio as gr
gr.close_all()

exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
_CANCEL_COLAB = False
_CURRENT_SHARE_URL = ""
_COLAB_HTML_CACHE = {}


import html


def render_audio_html(file_path, label="오디오 재생", autoplay=False):
    """원본 44.1kHz 무손실 Base64 오디오 플레이어 (인메모리 캐싱 적용)"""
    if not file_path or not os.path.exists(str(file_path)):
        return f"<div style='padding:10px; border:1px solid #d1d5db; border-radius:6px; color:#6b7280; font-size:13px;'>[{label}] 대본을 입력하고 '음성 생성' 버튼을 누르면 여기에 재생 바가 나타납니다.</div>"
    try:
        p = Path(file_path)
        st = p.stat()
        cache_key = (str(p.resolve()), st.st_mtime_ns, st.st_size, label, autoplay)
        if not autoplay and cache_key in _COLAB_HTML_CACHE:
            return _COLAB_HTML_CACHE[cache_key]

        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        auto_attr = "autoplay" if autoplay else ""
        safe_name = html.escape(p.name, quote=True)
        safe_label = html.escape(label, quote=True)
        card_html = f"""
        <div class="colab-aud-card" style="padding:10px 12px; border:1px solid #d1d5db; border-radius:6px; background:#ffffff;">
            <div style="display:flex; justify-content:space-between; margin-bottom:6px; font-size:13px; color:#374151;">
                <span style="font-weight:600;">{safe_label} — {safe_name}</span>
                <a href="#" data-fn="{safe_name}" onclick="const c=this.closest('.colab-aud-card');const a=document.createElement('a');a.href=c.querySelector('audio').src;a.download=this.dataset.fn;a.click();return false;" style="color:#111827; text-decoration:underline; font-size:12px;">다운로드</a>
            </div>
            <audio controls {auto_attr} preload="auto" style="width:100%; height:40px; display:block;" src="data:audio/wav;base64,{b64}"></audio>
        </div>
        """
        if not autoplay:
            _COLAB_HTML_CACHE[cache_key] = card_html
        return card_html
    except Exception as e:
        return f"<div>오류: {e}</div>"


def get_sample_list():
    ensure_default_sample()
    files = sorted([f.name for f in SAMPLE_DIR.iterdir() if f.is_file() and f.suffix.lower() in exts])
    return files if files else [DEFAULT_VOICE_NAME]


def load_saved_ref_text(voice_name):
    if not voice_name or voice_name.startswith("("):
        return DEFAULT_REF_TEXT
    txt_path = SAMPLE_DIR / (Path(voice_name).stem + ".txt")
    if txt_path.exists():
        txt = txt_path.read_text(encoding="utf-8").strip()
        if txt and txt != OLD_SHORT_REF_TEXT and not txt.endswith(","):
            return txt
    return DEFAULT_REF_TEXT


def on_colab_voice_change(voice_name):
    ensure_default_sample()
    p = SAMPLE_DIR / (voice_name or DEFAULT_VOICE_NAME)
    return render_audio_html(p, "샘플 미리듣기", autoplay=False), load_saved_ref_text(voice_name)


def refresh_voices():
    choices = get_sample_list()
    first = choices[0] if choices else None
    p = SAMPLE_DIR / (first or DEFAULT_VOICE_NAME)
    return gr.update(choices=choices, value=first), render_audio_html(p, "샘플 미리듣기", autoplay=False), load_saved_ref_text(first)


def cancel_colab_gen():
    global _CANCEL_COLAB
    _CANCEL_COLAB = True
    return "⏹ 취소됨"


def shutdown_colab_runtime():
    try:
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/jinpp00_fish_tts_colab_2026", data=b"RESET_OLD_COLAB", method="POST"), timeout=3)
    except Exception:
        pass

    def _unassign():
        time.sleep(1.0)
        try:
            from google.colab import runtime
            runtime.unassign()
        except Exception:
            os._exit(0)

    threading.Thread(target=_unassign, daemon=True).start()
    return "코랩 GPU 세션을 완전히 반납(종료)했습니다."


def generate_tts(voice_name, uploaded_file, ref_text, target_text, temp, top_p, rep_penalty):
    global _CANCEL_COLAB
    _CANCEL_COLAB = False
    t0 = time.time()
    if not target_text or not target_text.strip():
        return None, "대본을 입력해 주세요.", render_audio_html(None, "생성 결과")

    if uploaded_file is not None:
        src_p = Path(uploaded_file)
        saved_p = SAMPLE_DIR / src_p.name
        try:
            if src_p.resolve() != saved_p.resolve():
                shutil.copy2(src_p, saved_p)
        except Exception:
            pass
        audio_path = src_p
        v_label = src_p.stem
    else:
        ensure_default_sample()
        if not voice_name or voice_name.startswith("("):
            voice_name = DEFAULT_VOICE_NAME
        audio_path = SAMPLE_DIR / voice_name
        if not audio_path.exists():
            cands = list(SAMPLE_DIR.glob("*.wav"))
            audio_path = cands[0] if cands else audio_path
        v_label = Path(voice_name).stem

    clean_ref = (ref_text or "").strip()
    if not clean_ref or clean_ref == OLD_SHORT_REF_TEXT or clean_ref.endswith(","):
        clean_ref = DEFAULT_REF_TEXT

    audio_bytes = get_colab_ref_bytes(audio_path)
    target_clean = target_text.strip()
    dynamic_max_tokens = min(512, max(96, int(len(target_clean) * 6.5) + 48))

    torch.manual_seed(42)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(42)
    req = ServeTTSRequest(
        text=target_clean,
        references=[ServeReferenceAudio(audio=audio_bytes, text=clean_ref)],
        max_new_tokens=dynamic_max_tokens,
        chunk_length=200,
        top_p=float(top_p),
        repetition_penalty=float(rep_penalty),
        temperature=float(temp),
        format="wav",
        use_memory_cache="on",
        seed=42,
    )

    for result in tts_engine.inference(req):
        if _CANCEL_COLAB:
            return None, "⏹ 취소됨", render_audio_html(None, "생성 결과")
        if result.code == "final":
            sr, wav_data = result.audio
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            clean_preview = re.sub(r"[^가-힣a-zA-Z0-9]", "", target_clean)[:14] or "tts"
            v_slug = re.sub(r"[^가-힣a-zA-Z0-9_-]", "", v_label)[:20] or "voice"
            out_filename = f"{now_str}_{v_slug}_{clean_preview}.wav"
            out_path = OUTPUT_DIR / out_filename
            sf.write(str(out_path), wav_data, sr, subtype="PCM_16")
            return str(out_path), f"✅ 완료 ({time.time()-t0:.1f}초): {out_filename}", render_audio_html(out_path, "생성 결과", autoplay=True)
        elif result.code == "error":
            return None, f"오류: {result.error}", render_audio_html(None, "생성 결과")

    return None, "생성 실패", render_audio_html(None, "생성 결과")


init_sample_name = get_sample_list()[0]

with gr.Blocks(title="AI 음성 복제 (코랩)") as demo:
    mode_str = "T4 GPU 고속 모드" if device == "cuda" else "CPU 고속 모드"
    with gr.Row():
        gr.Markdown(f"### AI 음성 복제 스튜디오 ({mode_str})")
        shutdown_btn = gr.Button("코랩 세션 완전 종료 (반납)", size="sm", scale=0)

    with gr.Row():
        with gr.Column(scale=4):
            voice_dropdown = gr.Dropdown(
                choices=get_sample_list(),
                value=init_sample_name,
                allow_custom_value=True,
                label="샘플 목소리",
            )
            sample_player_html = gr.HTML(value=render_audio_html(None, "샘플 미리듣기"))
            refresh_btn = gr.Button("목록 새로고침", size="sm")
            with gr.Accordion("샘플 추가 / 원본 대사", open=False):
                ref_text_box = gr.Textbox(value=load_saved_ref_text(init_sample_name), lines=2, label="샘플 대사")
                upload_box = gr.Audio(sources=["upload"], type="filepath", label="새 샘플 파일")

        with gr.Column(scale=6):
            target_text_box = gr.Textbox(
                value="안녕하세요. 테스트 음성입니다.",
                lines=4,
                label="대본 입력",
            )
            with gr.Row():
                gen_btn = gr.Button("음성 생성", variant="primary", scale=3)
                stop_btn = gr.Button("정지 (취소)", variant="stop", scale=1)
            status_box = gr.Textbox(label="상태", lines=1, interactive=False)
            output_player_html = gr.HTML(value=render_audio_html(None, "생성 결과"))
            audio_out = gr.Audio(label="API 연동용 오디오", visible=False)

            with gr.Accordion("세부 설정", open=False):
                temp_slider = gr.Slider(0.1, 1.0, value=0.6, step=0.05, label="Temperature")
                top_p_slider = gr.Slider(0.1, 1.0, value=0.6, step=0.05, label="Top-P")
                rep_slider = gr.Slider(0.9, 2.0, value=1.2, step=0.05, label="Repetition Penalty")

    voice_dropdown.change(fn=on_colab_voice_change, inputs=[voice_dropdown], outputs=[sample_player_html, ref_text_box], queue=False)
    refresh_btn.click(fn=refresh_voices, inputs=[], outputs=[voice_dropdown, sample_player_html, ref_text_box], queue=False)
    gen_ev = gen_btn.click(
        fn=generate_tts,
        inputs=[voice_dropdown, upload_box, ref_text_box, target_text_box, temp_slider, top_p_slider, rep_slider],
        outputs=[audio_out, status_box, output_player_html],
        api_name="generate_tts",
    )
    stop_btn.click(fn=cancel_colab_gen, inputs=[], outputs=[status_box], cancels=[gen_ev], queue=False)
    shutdown_btn.click(fn=shutdown_colab_runtime, inputs=[], outputs=[status_box], api_name="shutdown_colab", queue=False)
    demo.load(fn=lambda: render_audio_html(SAMPLE_DIR / init_sample_name, "샘플 미리듣기", False), inputs=[], outputs=[sample_player_html], queue=False)

_, _, share_url = demo.launch(
    share=True,
    inline=True,
    prevent_thread_lock=True,
    theme=gr.themes.Monochrome(),
    allowed_paths=[str(SAMPLE_DIR), str(OUTPUT_DIR), "/tmp"],
)
if share_url:
    _CURRENT_SHARE_URL = share_url.strip()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "https://ntfy.sh/jinpp00_fish_tts_colab_2026",
                data=_CURRENT_SHARE_URL.encode("utf-8"),
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            ),
            timeout=5,
        )
    except Exception:
        pass
    print(f"✅ 내 PC 스튜디오 자동 연결 완료: {_CURRENT_SHARE_URL}")
