# AI 음성 복제 스튜디오 (TTS Studio — Fish Speech 1.5)

무료 오픈소스 **Fish Speech 1.5** 기반의 한국어 AI 음성 복제(Zero-Shot Voice Cloning) 로컬 & 구글 코랩 하이브리드 스튜디오입니다.

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/jinPP00/tts_studio/blob/main/fish_tts_studio.ipynb)

## 주요 기능

- **코랩 T4 GPU 1순위 + 내 PC 로컬 엔진 자동 백업**
  - 코랩이 켜져 있으면 초고속 T4 GPU 서버로 생성하고, 코랩이 꺼져 있으면 1.5초 만에 감지하여 재시도 대기 없이 내 PC 로컬 `Fish Speech 1.5` 엔진으로 즉시 전환합니다.
- **드라이브 권한 팝업 없음 & T4 GPU 자동 고정**
  - 코랩 노트북(`fish_tts_studio.ipynb`)에 T4 GPU 메타데이터가 내장되어 있으며, 번거로운 구글 드라이브 마운트 권한 팝업 없이 코랩 내부 SSD에서 즉시 구동됩니다.
- **실시간 취소(정지) 및 코랩 세션 반납**
  - 생성 도중 언제든 `정지 (취소)` 버튼으로 즉시 중단할 수 있으며, 작업 완료 후 `코랩 세션 반납` 버튼으로 코랩 GPU 세션을 깔끔하게 종료하여 *"활성 세션이 너무 많습니다"* 오류를 방지합니다.
- **원하는 로컬 폴더에 즉시 저장 & 브라우저 기본 오디오 플레이어**
  - 바탕화면(`TTS_완성본`), 다운로드 폴더, 구글 드라이브 또는 원하는 임의 폴더에 즉시 `.wav` 파일로 저장됩니다.
  - 브라우저 표준 HTML5 `<audio controls>` 플레이어와 `윈도우 기본 플레이어로 재생` 버튼을 탑재해 끊김 없는 재생을 보장합니다.

## 파일 구성

- `app.py` — 내 PC 로컬 웹 스튜디오 (`http://127.0.0.1:7865`)
- `launch.py` / `무료_AI_음성복제_스튜디오.bat` — 원클릭 자동 실행 런처
- `fish_tts_studio.ipynb` — 구글 코랩 T4 GPU 원클릭 서버 노트북
- `update_nb.py` — 코랩 노트북 생성 및 업데이트 스크립트
- `샘플목소리/` — 기본 참조 목소리 샘플 및 대사 텍스트

## 실행 방법

1. `무료_AI_음성복제_스튜디오.bat` 실행 (또는 `python app.py`)
2. 코랩 가속 사용 시 상단 **[코랩 열기]** 클릭 후 노트북의 **▶ 코랩 엔진 켜기** 버튼 1회 클릭
3. 대본 입력 후 **[음성 생성]** 클릭
