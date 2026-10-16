# AI 음성 복제 스튜디오 (TTS Studio — Fish Speech 1.5)

무료 오픈소스 **Fish Speech 1.5** 기반의 한국어 AI 음성 복제(Zero-Shot Voice Cloning) 로컬 & 구글 코랩 하이브리드 스튜디오입니다.  
이 저장소(`https://github.com/jinPP00/tts_studio.git`)를 어느 컴퓨터에서나 `git clone` / `git pull` 받으면 별도 설정 없이 100% 동일하게 동작합니다.

---

## 🔗 바로 접속 주소 (경고 팝업 없음 · 단일 세션 고정)

* **구글 코랩 원클릭 서버 바로가기 (내 구글 드라이브 원본 — 외부 노트북 경고 없음):**  
  👉 **[https://colab.research.google.com/drive/1UKsxGyCwji6kTzov8D_3rUvJT0CnyQLb](https://colab.research.google.com/drive/1UKsxGyCwji6kTzov8D_3rUvJT0CnyQLb)**
* **GitHub 미러 코랩 주소 (다른 구글 계정 사용 시):**  
  👉 [https://colab.research.google.com/github/jinPP00/tts_studio/blob/main/fish_tts_studio.ipynb](https://colab.research.google.com/github/jinPP00/tts_studio/blob/main/fish_tts_studio.ipynb)
* **GitHub 저장소 주소:**  
  👉 **[https://github.com/jinPP00/tts_studio](https://github.com/jinPP00/tts_studio)**

---

## 💻 다른 컴퓨터(새 PC)에서 똑같이 사용하는 방법

### 방법 A. `git clone` / `git pull` 후 실행 (권장)
```bash
git clone https://github.com/jinPP00/tts_studio.git C:\projects\tts_studio
cd C:\projects\tts_studio
무료_AI_음성복제_스튜디오.bat
```
- 이미 클론된 PC에서는 폴더 안의 **`무료_AI_음성복제_스튜디오.bat`**만 더블클릭하면 자동으로 `git pull`을 수행하여 최신 버전 및 접속 주소를 동기화하고 `http://127.0.0.1:7865` 스튜디오를 실행합니다.
- 폴더 안의 **`코랩_웹스튜디오_바로가기.url`** (또는 **`코랩_서버_열기.bat`**)을 더블클릭하면 `"이 노트북은 Google에서 작성하지 않았습니다"` 경고 팝업이 전혀 뜨지 않는 내 드라이브 고정 코랩 주소(`drive/1UKsxGyCwji6kTzov8D_3rUvJT0CnyQLb`)가 즉시 열립니다.
- 새 PC에서 코랩이 꺼져 있을 때 `내 PC 로컬` 모드로 생성하면, `fish-speech` 엔진과 1.2GB 모델 가중치(`model.pth`)까지 자동으로 다운로드·설치되어 동일하게 작동합니다.

### 방법 B. 설치 없이 웹브라우저에서만 사용
- 파이썬·Git이 없는 외부 PC에서는 위 **구글 코랩 바로가기 주소(`drive/1UKsxGyCwji6kTzov8D_3rUvJT0CnyQLb`)**만 열면 코랩 화면 내부에서 샘플 미리듣기·음성 복제·WAV 다운로드를 모두 동일하게 사용할 수 있습니다.
