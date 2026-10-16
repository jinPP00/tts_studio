import socket
import webbrowser
import subprocess
import sys

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

if is_port_in_use(7865):
    print("Studio is already running! Opening browser -> http://127.0.0.1:7865")
    webbrowser.open("http://127.0.0.1:7865")
else:
    print("Starting AI Voice Cloning Studio -> http://127.0.0.1:7865 ...")
    subprocess.run([sys.executable, r"C:\projects\tts_studio\app.py"])