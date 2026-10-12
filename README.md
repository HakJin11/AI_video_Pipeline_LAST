# AI 영상 파이프라인 — 유튜브 따라하기 (서버 / 클라이언트)

짧은 유튜브 장면(10/20/30초)을 1~2초 컷으로 나누고, 컷마다 **다른 인물·목소리·배경**으로 같은 장면을 AI로 다시
만드는 프로그램입니다. 마지막에 원본과 AI 영상을 나란히 놓은 비교 영상을 만들어 줍니다.

```
┌──────────── 클라이언트 PC (아무 Windows) ───────────┐        ┌──────────── 서버 PC (NVIDIA GPU 16GB) ─────────────┐
│ client/AIVideoClient.exe                          │  UDP   │ server/udp_server/AIVideoServer.exe  (포트 9999)     │
│  · 영상 고르기·만들기 시작·진행 상황·영상 받기        │ ◀────▶ │        │ HTTP                                       │
│  · 🌐 웹 화면 열기 → 이 PC 브라우저에서 모든 설정     │  9999  │ server/app  웹 앱 (127.0.0.1:8010)                 │
└───────────────────────────────────────────────────┘        │        ├─ ComfyUI  (Qwen 사진 / Qwen3-TTS 목소리 / LTX 영상) │
                                                              │        ├─ Ollama gemma4:12b (프롬프트 정리·번역)          │
                                                              │        └─ ffmpeg (자르기·합치기)                         │
                                                              └─────────────────────────────────────────────────────┘
```

| 폴더 | 어디에 두나 | 설명 |
|---|---|---|
| [`server/`](server/README.md) | **서버 PC** (그래픽카드 있는 컴퓨터) | 영상 앱, ComfyUI 워크플로, 인물·목소리·배경 데이터, UDP 서버 |
| [`client/`](client/README.md) | **클라이언트 PC** (사용하는 사람 컴퓨터) | `AIVideoClient.exe` 하나면 됨 (Python 없이 실행) |

### 바이브 코딩(AI 코딩 도구)으로 설치하기

각 폴더에 AI 도구용 안내 파일(`AGENTS.md`, `CLAUDE.md`)이 들어 있습니다. Claude Code / Codex / Cursor 등으로
**해당 폴더를 열고** 이렇게 말하면 됩니다.

- 서버 PC에서 `server/` 폴더를 열고: **"AGENTS.md 보고 이 서버 설치하고 실행해 줘"**
- 클라이언트 PC에서 `client/` 폴더를 열고: **"AGENTS.md 보고 클라이언트 실행해 줘. 서버 IP는 192.168.x.x야"**

### 순서 요약

1. 서버 PC: [`server/README.md`](server/README.md)대로 ffmpeg·Ollama·ComfyUI·모델 설치 → `start_app.bat` → 
   `udp_server\start_udp_server.bat` (비밀번호는 `udp_server\server_config.json`) → 방화벽 UDP 9999 허용
2. 클라이언트 PC: `client\AIVideoClient.exe` 실행 → 서버 IP·포트·비밀번호 → **연결** → **🌐 웹 화면 열기**

### 들어 있는 데이터

- 인물 9명(실존 인물 아님, AI 생성 얼굴), 목소리 20개(Qwen3-TTS VoiceDesign으로 만든 가상 목소리, 말투 클립 포함),
  배경 41개, 프리셋 1개(썸사친에 보드게임), 테스트로 검증된 동작 14개
- 원본 유튜브 영상과 실존 인물 사진·녹음 목소리는 들어 있지 않습니다. 원본 영상은 서버에서
  `scripts\restore_preset_video.py`로 직접 내려받습니다.
