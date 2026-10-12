# 서버 (server/) — 그래픽카드 있는 PC에 설치

영상 앱(웹 화면 + API), ComfyUI 워크플로, 데이터(인물·목소리·배경·프리셋), 그리고 다른 PC가 UDP로 접속하는
**UDP 서버**가 들어 있습니다. 클라이언트 PC에는 `../client/`만 주면 됩니다.

> AI 코딩 도구로 설치하려면 이 폴더를 열고 "AGENTS.md 보고 설치하고 실행해 줘"라고 하세요.

```
영상 만드는 흐름
인물 사진 + 배경 ──(ComfyUI: Qwen-Image-Edit)──▶ 컷마다 첫 장면 사진
대사 ──(ComfyUI: Qwen3-TTS 목소리 복제/말투)──▶ 컷마다 음성
사진 + 음성 + 프롬프트 ──(ComfyUI: LTX-2.3)──▶ 립싱크 영상 (1~2초)
프롬프트 정리·번역·행동 고르기 ──(Ollama gemma4:12b, 항상 GPU)
모든 컷 ──(ffmpeg)──▶ 완성 영상 + 원본|AI 비교 영상
```

## 1. 폴더 구성

| 경로 | 내용 |
|---|---|
| `app/` | 서버 코드 (FastAPI). `app/workflows/`는 앱이 ComfyUI에 보내는 API 형식 워크플로 |
| `frontend/` | 웹 화면 (index.html, follow.js, styles.css) |
| `data/app.db` | SQLite DB: 프리셋 1개, 인물 9명, 목소리 20개, 말투 16종, 배경 41개, 동작 18개(✅검증 14) |
| `media/` | 인물 사진, 목소리 원본·말투 클립, 배경 사진 |
| `comfyui_workflows/` | ComfyUI 화면에서 직접 여는 워크플로 3개 (사진 합성 / 목소리 복제 / 영상) |
| `scripts/` | `restore_preset_video.py`(원본 영상 복원), `make_characters.py`(가상 인물 생성), `test_motions.py`(동작 테스트) |
| `udp_server/` | UDP 서버: `AIVideoServer.exe`, `start_udp_server.bat`, `server_config.example.json`, 소스 |
| `start_app.bat` | 앱 실행 (감시 프로그램이 앱·ComfyUI가 꺼지면 다시 켬) |

## 2. 필요한 환경

| 항목 | 권장 (실제 테스트 환경) |
|---|---|
| OS | Windows 10/11 64bit |
| GPU | NVIDIA, VRAM 16 GB (RTX 4070 Ti SUPER) |
| RAM | 32 GB |
| 디스크 | 모델 약 100 GB + 여유 20 GB |
| 소프트웨어 | ComfyUI Desktop, Ollama, ffmpeg, Python 3.11 이상 |

## 3. 설치

### 3-1. ffmpeg
1. https://www.gyan.dev/ffmpeg/builds/ 의 *release essentials* zip을 `C:\ffmpeg`에 풉니다 (`C:\ffmpeg\bin\ffmpeg.exe`).
2. 다른 곳이면 PATH에 넣거나 환경변수 `FFMPEG_PATH`, `FFPROBE_PATH`를 설정합니다.

### 3-2. Ollama
```bat
:: https://ollama.com/download 에서 설치 후
ollama pull gemma4:12b
```
`http://127.0.0.1:11434`가 응답하면 됩니다.

### 3-3. ComfyUI Desktop + 커스텀 노드 + 모델
1. ComfyUI Desktop 설치: https://www.comfy.org/download
2. ComfyUI Manager로 커스텀 노드 설치: `ComfyUI-LTXVideo`, `ComfyUI-QwenTTS`, `ComfyUI-KJNodes`,
   `ComfyUI-VideoHelperSuite`, `rgthree-comfy`, `comfyui-ollama`
3. 모델 (ComfyUI `models/` 아래). `comfyui_workflows/`의 json을 ComfyUI에 끌어다 놓으면 Manager의
   **Install Missing Models**로 대부분 받을 수 있습니다.

| 폴더 | 파일 | 용량 | 쓰는 곳 |
|---|---|---|---|
| checkpoints | `ltx-2.3-22b-dev-fp8.safetensors` | 28 GB | 영상 |
| loras | `ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors` | 2.6 GB | 영상 |
| latent_upscale_models | `ltx-2.3-spatial-upscaler-x2-1.1.safetensors` | 950 MB | 영상 |
| text_encoders | `gemma_3_12B_it_fp4_mixed.safetensors` | 8.8 GB | 영상 |
| loras | `gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors` | 600 MB | 영상 |
| diffusion_models | `qwen_image_edit_2511_fp8mixed.safetensors` | 20 GB | 인물 사진 |
| text_encoders | `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 8.8 GB | 인물 사진 |
| loras | `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | 811 MB | 인물 사진 |
| vae | `qwen_image_vae.safetensors` | 243 MB | 인물 사진 |
| diffusion_models | `z_image_turbo_bf16.safetensors` | 12 GB | 배경·가상 인물 그리기 |
| text_encoders | `qwen_3_4b.safetensors` | 8 GB | 배경·가상 인물 그리기 |
| vae | `ae.safetensors` | 335 MB | 배경·가상 인물 그리기 |
| TTS | `Qwen3-TTS-12Hz-1.7B-Base` (폴더) | 3.6 GB | 목소리 (QwenTTS 노드가 처음 실행 때 자동 다운로드) |

4. ComfyUI의 Python(`ComfyUI\.venv\Scripts\python.exe`)에 패키지 버전을 맞춥니다:
```bat
python -m pip install "transformers==4.57.6" "diffusers==0.35.2" "huggingface-hub==0.36.2" "kornia==0.8.2"
python -m pip install sox onnxruntime openai-whisper
```
   (QwenTTS 노드가 최신 transformers와 안 맞고, kornia 0.8.3은 LTXVideo 노드를 깨뜨림. whisper는 만든 목소리 검사용)
5. `http://127.0.0.1:8188/queue`가 열리는지 확인 (8188이 사용 중이면 8189~8191도 자동으로 찾음).

### 3-4. 앱 Python 환경
```bat
cd server
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

### 3-5. 경로 설정 (기본값과 다를 때만)

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `COMFYUI_INPUT_DIR` | `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared\input` | ComfyUI input 폴더 |
| `COMFYUI_OUTPUT_DIR` | `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared\output` | ComfyUI output 폴더 |
| `COMFYUI_PYTHON` | `ComfyUI (1)\ComfyUI\.venv\Scripts\python.exe` | Whisper를 돌릴 ComfyUI Python |
| `COMFYUI_DIR` | `ComfyUI (1)\ComfyUI` | 감시 프로그램이 ComfyUI를 직접 켤 때의 폴더 |
| `FFMPEG_PATH`, `FFPROBE_PATH` | `C:\ffmpeg\bin\...` → 없으면 PATH | ffmpeg 위치 |

### 3-6. 프리셋 원본 영상 복원 (처음 한 번)
원본 유튜브 영상은 들어 있지 않습니다. 저장된 유튜브 주소·시간대로 내려받아 컷을 다시 자릅니다:
```bat
.venv\Scripts\python scripts\restore_preset_video.py
```

## 4. 실행

### 4-1. 영상 앱
```bat
start_app.bat
```
- 확인: http://127.0.0.1:8010/api/health → `{"comfyui": true, "ollama": true}`
- 서버 PC에서 직접 쓸 때는 브라우저로 http://127.0.0.1:8010
- 감시 없이 켜기: `.venv\Scripts\python -m uvicorn app.main:app --port 8010 --host 127.0.0.1`
- 앱은 이 PC 안(127.0.0.1)에서만 열립니다. 다른 PC는 아래 UDP 서버로 접속합니다.

### 4-2. UDP 서버 (다른 PC 접속용)
1. `udp_server\server_config.example.json`을 `server_config.json`으로 복사하고 **비밀번호를 바꿉니다**
   (`start_udp_server.bat`이 처음 실행 때 자동으로 복사함):
```json
{ "port": 9999, "password": "원하는비밀번호", "app": "http://127.0.0.1:8010" }
```
2. `udp_server\start_udp_server.bat` 실행 → 창에 **클라이언트에 알려 줄 이 PC의 IP**가 나옵니다.
   접속한 클라이언트 IP와 명령이 이 창에 기록됩니다.
3. Windows 방화벽에서 **UDP 9999 인바운드 허용** (처음 실행 때 뜨는 창에서 *개인 네트워크* 허용, 또는 관리자 PowerShell):
```powershell
New-NetFirewallRule -DisplayName "AIVideo UDP" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow
```
- exe 대신 소스로: `python udp_server\udp_server.py --port 9999 --password 비밀번호` (Python 표준 라이브러리만 사용)
- 다시 빌드: `udp_server\build.bat`

### 클라이언트에서 모든 설정을 하는 원리
클라이언트의 **🌐 웹 화면 열기**는 클라이언트 PC에 작은 중계(127.0.0.1:8765)를 켜고 브라우저를 엽니다. 브라우저 요청은
전부 UDP로 이 서버에 와서 영상 앱(8010)에 전달되고, 답(화면·사진·영상)은 조각으로 나눠 돌아갑니다. 그래서 서버 PC에서
보는 화면과 **똑같은 화면**(배경·인물·목소리·컷마다 대사/동작/말투/구도/카메라/프롬프트, 사진 올리기, 관리자 화면)을
클라이언트에서 쓸 수 있습니다. 앱의 8010 포트는 밖으로 열지 않아도 됩니다.

UDP 명령: `ping`, `catalog`, `runs`, `start`, `render`, `status`, `assemble`, `file_info`, `chunks`,
`http`(웹 요청 중계), `upload_part`(큰 요청 본문). 같은 id의 요청은 한 번만 실행되고, 큰 데이터는 8000바이트 조각 +
SHA-256 확인. 측정: 손실 없음 2 MB 0.1초 / 패킷 20% 손실 2 MB 13초 (모두 원본과 동일).

## 5. 사용 방법 (웹 화면)

1. **영상 고르기**: 프리셋을 고르고 **▶ 10초 / 20초 / 30초 만들기**
2. **배경**: 추천 배경을 누르거나 장소를 한국어로 대충 쓰고 **🎨 배경 사진 만들기** (서버 PC에서 무료로 그림).
   배경을 바꾸면 인물 사진을 바로 미리 그리기 시작 (3~5분)
3. **등장인물**: 역할(A~E)마다 인물·목소리 선택. **+ 내 사진으로 이 역할 하기**로 본인(또는 동의받은 사람) 사진 사용
4. **컷 편집**: 대사, 시선, **✅ 검증된 동작**(손 흔들기·박수·어깨 으쓱·팔짱·가리키기·손사래·양손 번쩍·주먹 불끈·엄지 척·
   손가락 하나·머리 긁적·웃음 터짐·앞으로 쑥·깜짝 놀람), 말투 16종, 인원, 샷 크기, 카메라, 프롬프트.
   한국어로 쓴 행동은 뜻 그대로 번역됨. **🎬 AI가 행동 정하기**는 대사에 맞는 검증된 동작을 고름.
   **📜 예전에 쓴 프롬프트**에서 이전 영상·프롬프트를 보고 같은 시드로 다시 쓰기
5. **만들기**: ⏱ 상자의 **▶ 지금 남은 컷 전부 만들기** 또는 컷마다 **만들고 다음 컷 →**. 진행 막대에 지금 하는 일과
   남은 시간. 다 되면 자동으로 합쳐서 **원본 | AI 비교 영상**
6. 고르는 동안 사진→목소리→영상을 미리 만들어 둠(⚡). 같은 설정은 저장된 것을 바로 씀

| 길이 (고화질) | 기본 배경 | 새 배경 |
|---|---|---|
| 10초 (8컷) | 약 7~9분 | 약 13분 |
| 20초 (16컷) | 약 15분 | 약 18~22분 |
| 30초 (23컷) | 약 20분 | 약 28~30분 |

**관리자 모드** `/?admin=1`: 유튜브 주소·시작·길이로 새 프리셋 만들기, 다듬은 뒤 "공개"하면 첫 화면에 표시.
**가상 인물 추가**: `scripts\make_characters.py`의 `PEOPLE`에 이름·성별·생김새·목소리를 넣고 실행 (ComfyUI 필요).

## 6. 문제 해결

| 증상 | 확인 |
|---|---|
| health에서 comfyui false | ComfyUI가 켜졌는지, 8188~8191 중 하나가 열렸는지. Comfy Desktop 창을 두 개 띄우지 말 것 |
| ollama false | `ollama serve`, `ollama list`에 gemma4:12b |
| 그림이 0.01초 만에 "성공"하고 결과 없음 | `COMFYUI_INPUT_DIR / OUTPUT_DIR`가 실제 ComfyUI 폴더인지 |
| 갑자기 컷당 2배 이상 느림 | ComfyUI를 며칠 켜 두면 느려짐 → 앱이 쉬는 동안 자동으로 다시 켬 (화면의 🐢 경고) |
| 목소리가 안 나옴 | 3-3의 ComfyUI 패키지 버전 |
| 클라이언트가 접속 못 함 | 방화벽 UDP 9999, 같은 네트워크인지, `server_config.json` 비밀번호 |
| 프리셋 컷 사진이 안 보임 | `scripts\restore_preset_video.py` 실행 |
