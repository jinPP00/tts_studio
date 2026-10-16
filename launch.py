import os
import sys
import socket
import webbrowser
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_PATH = BASE_DIR / "app.py"


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_dependencies():
    try:
        import gradio  # noqa: F401
        import gradio_client  # noqa: F401
        import soundfile  # noqa: F401
        import librosa  # noqa: F401
    except ImportError:
        print("필수 패키지 자동 설치 중 (최초 1회)...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "gradio", "gradio_client", "soundfile", "librosa", "numpy"],
            check=False,
        )


if __name__ == "__main__":
    if is_port_in_use(7865):
        print("스튜디오가 이미 실행 중입니다! 브라우저를 엽니다 -> http://127.0.0.1:7865")
        webbrowser.open("http://127.0.0.1:7865")
    else:
        ensure_dependencies()
        print("AI 음성 복제 스튜디오를 시작합니다 -> http://127.0.0.1:7865 ...")
        webbrowser.open("http://127.0.0.1:7865")
        subprocess.run([sys.executable, str(APP_PATH)])