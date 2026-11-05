# 📋 AI 음성 복제 스튜디오 (tts_studio) — 기획 및 요구사항 정의서 (PRD)

> **프로젝트 경로**: `C:\projects\tts_studio`
> **서비스 설명**: Fish Speech 1.5 한국어 AI 음성 복제 (로컬 :7865 & 코랩)
> **운영 URL**: https://colab.research.google.com/drive/1UKsxGyCwji6kTzov8D_3rUvJT0CnyQLb

---

## 1. 서비스 개요 및 핵심 목표
- **서비스명**: AI 음성 복제 스튜디오
- **핵심 가치**: Fish Speech 1.5 한국어 AI 음성 복제 (로컬 :7865 & 코랩)
- **운영 환경**: local

## 2. 주요 기능 및 아키텍처 요약
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

## 3. 향후 추가 기획 및 개선 범위
- **다음 목표**: 참조 음성 샘플별 억양·속도 프리셋 정리
- 신규 대규모 기능 추가 시 본 `PRD.md`에 요구사항과 수정 대상 파일 범위를 먼저 업데이트한 후 구현을 진행합니다.
