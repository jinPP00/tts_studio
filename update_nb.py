import json
from pathlib import Path

colab_server_code = Path(r"C:\projects\tts_studio\colab_server.py").read_text(encoding="utf-8")

# 노트북 셀은 항상 GitHub의 최신 colab_server.py를 먼저 받아와 실행하고, 오프라인/실패 시에만 내장 코드를 실행함
cell_code = (
    "#@title ▶ 코랩 엔진 켜기 (클릭 한 번만 하세요 · 경고/권한 팝업 없음 · 최신 코드 자동 동기화)\n"
    "import time, urllib.request\n"
    "_ran_remote = False\n"
    "try:\n"
    "    _url = f'https://raw.githubusercontent.com/jinPP00/tts_studio/main/colab_server.py?t={int(time.time())}'\n"
    "    _req = urllib.request.Request(_url, headers={'User-Agent': 'Mozilla/5.0'})\n"
    "    _code = urllib.request.urlopen(_req, timeout=10).read().decode('utf-8')\n"
    "    if 'TTSInferenceEngine' in _code:\n"
    "        _ran_remote = True\n"
    "        exec(compile(_code, 'colab_server.py', 'exec'), globals())\n"
    "except Exception as _e:\n"
    "    print('원격 스크립트 로드 폴백:', _e)\n"
    "if not _ran_remote:\n"
    + "\n".join("    " + line if line.strip() else "" for line in colab_server_code.splitlines())
)

nb = {
    "nbformat": 4,
    "nbformat_minor": 0,
    "metadata": {
        "colab": {"provenance": [], "gpuType": "T4"},
        "accelerator": "GPU",
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {"id": "header_cell"},
            "source": [
                "### AI 음성 복제 스튜디오 (코랩 서버)\n",
                "* 아래 **▶ 코랩 엔진 켜기** 버튼만 한 번 누르시면 항상 최신 버전으로 바로 켜집니다.",
            ],
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {"id": "run_cell", "cellView": "form"},
            "outputs": [],
            "source": [line + "\n" for line in cell_code.splitlines()],
        },
    ],
}

out_json = json.dumps(nb, ensure_ascii=False, indent=2)

for target in [
    Path(r"G:\내 드라이브\TTS\Fish_Speech_무료음성복제.ipynb"),
    Path(r"G:\내 드라이브\Fish_Speech_무료음성복제.ipynb"),
    Path(r"C:\projects\tts_studio\fish_tts_studio.ipynb"),
]:
    try:
        target.write_text(out_json, encoding="utf-8")
        print("Updated:", target)
    except Exception as e:
        print("Skip:", target, e)

try:
    v2_tmp = Path(r"G:\내 드라이브\TTS\AI_음성복제_스튜디오_v2.ipynb")
    if v2_tmp.exists():
        v2_tmp.unlink()
except Exception:
    pass
