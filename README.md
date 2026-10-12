# AI 영상 파이프라인 — 유튜브 따라하기

짧은 유튜브 장면(10/20/30초)을 1~2초 컷으로 나누고, 컷마다 **다른 인물·목소리·배경**으로 같은 장면을 AI로 다시
만드는 웹 앱입니다. 마지막에 원본과 AI 영상을 나란히 놓은 비교 영상을 만들어 줍니다.

```
인물 사진 + 배경 ──(Qwen-Image-Edit)──▶ 컷마다 첫 장면 사진
대사 ──(Qwen3-TTS 목소리 복제/말투)──▶ 컷마다 음성
사진 + 음성 + 프롬프트 ──(LTX-2.3)──▶ 립싱크 영상 (1~2초)
프롬프트 정리·번역·행동 고르기 ──(Ollama gemma4:12b)
모든 컷 ──(ffmpeg)──▶ 완성 영상 + 원본|AI 비교 영상
```

- 웹 앱: FastAPI (포트 8010) + 순수 HTML/JS 화면
- 다른 PC에서 IP로 조종하는 **UDP 서버/클라이언트** 실행파일 포함 (`udp/`)
- ComfyUI 워크플로 포함 (`workflows/`, 앱이 쓰는 API용은 `app/workflows/`)

---

## 1. 폴더 구성

| 경로 | 내용 |
|---|---|
| `app/` | 서버 코드 (FastAPI). `app/workflows/`는 앱이 ComfyUI에 보내는 API 형식 워크플로 |
| `frontend/` | 웹 화면 (index.html, follow.js, styles.css) |
| `data/app.db` | SQLite DB: 프리셋 1개(썸사친에 보드게임), 인물 9명, 목소리 20개, 말투 16종, 배경 41개, 검증된 동작 18개(✅14) |
| `media/` | 인물 사진, 목소리 원본·말투 클립, 배경 사진 |
| `scripts/` | `restore_preset_video.py`(원본 영상 복원), `make_characters.py`(가상 인물 생성), `test_motions.py`(동작 테스트) |
| `udp/` | UDP 서버/클라이언트 소스와 실행파일 (`udp/release/*.exe`) |
| `workflows/` | ComfyUI에서 직접 여는 워크플로 3개 (사진 합성 / 목소리 복제 / 영상) |
| `start_app.bat` | 앱 실행 (꺼지면 자동으로 다시 켜는 감시 프로그램 포함) |

### 들어 있는 것 / 뺀 것

- **인물 9명**: 하승, 승현, 민지, 지수, 예린, 소윤, 도현, 태민, 건우 — 실존 인물이 아닌 AI 생성 얼굴
  (지수~건우 6명은 `scripts/make_characters.py`로 Z-Image-Turbo에서 생성)
- **목소리 20개**: 민재, 도윤, 현우, 준호, 지아, 서연, 하은, 유나, 태오, 시우, 다온, 채원, 상철, 미숙, 영감, 순자,
  동수, 은비, 재원, 소희 — 모두 Qwen3-TTS VoiceDesign으로 만든 가상 목소리, 목소리마다 말투 클립 포함
- **뺀 것**: 실제 사람 녹음 목소리, 개인 인물 사진, 실존 출연자 사진, 원본 유튜브 영상, 만든 영상·캐시 전부
  → 원본 영상은 아래 *3-6. 프리셋 원본 영상 복원*으로 각자 내려받습니다.

---

## 2. 필요한 환경

| 항목 | 권장 (실제 테스트 환경) |
|---|---|
| OS | Windows 10/11 64bit |
| GPU | NVIDIA, VRAM 16 GB (RTX 4070 Ti SUPER에서 테스트) |
| RAM | 32 GB |
| 디스크 | 모델 약 100 GB + 여유 20 GB |
| 소프트웨어 | ComfyUI Desktop, Ollama, ffmpeg, Python 3.11 이상 |

---

## 3. 설치

### 3-1. ffmpeg

1. https://www.gyan.dev/ffmpeg/builds/ 에서 *release essentials* zip을 받아 `C:\ffmpeg`에 풉니다
   (`C:\ffmpeg\bin\ffmpeg.exe`가 있어야 함).
2. 다른 곳에 설치했다면 PATH에 넣거나 환경변수 `FFMPEG_PATH`, `FFPROBE_PATH`로 위치를 알려 줍니다.

### 3-2. Ollama (프롬프트 정리·번역·행동 고르기)

```bat
:: https://ollama.com/download 에서 설치 후
ollama pull gemma4:12b
```

`http://127.0.0.1:11434`에서 응답하면 됩니다. gemma는 항상 GPU에서 돌아가고, 영상을 만드는 중이면 그 컷이 끝날 때까지
잠깐 기다렸다가 GPU를 씁니다 (CPU로 돌리면 몇 배 느려지고 영상 계산도 같이 느려짐).

### 3-3. ComfyUI Desktop + 커스텀 노드 + 모델

1. ComfyUI Desktop 설치: https://www.comfy.org/download
2. ComfyUI Manager에서 커스텀 노드 설치:
   `ComfyUI-LTXVideo`, `ComfyUI-QwenTTS`, `ComfyUI-KJNodes`, `ComfyUI-VideoHelperSuite`, `rgthree-comfy`, `comfyui-ollama`
3. 모델 (ComfyUI `models/` 아래 폴더). `workflows/`의 json을 ComfyUI에 끌어다 놓으면 Manager의
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

4. ComfyUI의 Python에 아래 패키지를 맞춰 줍니다 (QwenTTS 노드가 최신 transformers와 맞지 않음).
   ComfyUI 폴더의 `.venv\Scripts\python.exe`로 실행하세요.

```bat
python -m pip install "transformers==4.57.6" "diffusers==0.35.2" "huggingface-hub==0.36.2" "kornia==0.8.2"
python -m pip install sox onnxruntime openai-whisper
```

   - `kornia 0.8.3`은 ComfyUI-LTXVideo 로딩을 깨뜨려서 0.8.2로 고정
   - `openai-whisper`: 만든 목소리를 다시 들어서 깨진 발음을 걸러내는 데 사용
5. ComfyUI를 켜고 `http://127.0.0.1:8188/queue`가 열리는지 확인합니다 (8188이 사용 중이면 8189~8191도 자동으로 찾음).

### 3-4. 앱 Python 환경

```bat
cd 이_저장소_폴더
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

ComfyUI Desktop 기본 설치라면 대부분 그대로 두면 됩니다.

### 3-6. 프리셋 원본 영상 복원 (처음 한 번)

저장소에는 원본 유튜브 영상이 없습니다. 아래 명령으로 프리셋에 저장된 유튜브 주소·시작 시간·컷 시간대로 직접 내려받아
컷을 다시 자릅니다 (yt-dlp, ffmpeg 필요).

```bat
.venv\Scripts\python scripts\restore_preset_video.py
```

---

## 4. 실행

```bat
start_app.bat
```

- 몇 초 뒤 브라우저에서 **http://127.0.0.1:8010** 을 엽니다.
- `start_app.bat`은 숨은 감시 프로그램(`app_watchdog.ps1`)을 켭니다. 앱이 멈추면 다시 켜고, ComfyUI가 꺼져 있으면
  켭니다. 로그: `app_server.log`
- 감시 없이 직접 켜려면: `.venv\Scripts\python -m uvicorn app.main:app --port 8010 --host 127.0.0.1`
- 확인: http://127.0.0.1:8010/api/health → `{"comfyui": true, "ollama": true}`

---

## 5. 사용 방법

1. **영상 고르기**: 첫 화면에서 프리셋을 고르고 **▶ 10초 / 20초 / 30초 만들기**를 누릅니다.
2. **배경**: 추천 배경을 누르거나, 장소를 한국어로 대충 쓰고 **🎨 배경 사진 만들기**를 누릅니다 (이 PC에서 무료로 그림).
   배경을 바꾸면 인물 사진을 그 자리에서 미리 그리기 시작합니다 (3~5분).
3. **등장인물**: 역할(A~E)마다 인물과 목소리를 고릅니다. **+ 내 사진으로 이 역할 하기**로 본인(또는 동의받은 사람)
   사진도 쓸 수 있습니다.
4. **컷 편집** (위의 컷 타임라인에서 컷 선택)
   - 대사: 원본 길이에 맞는 글자 수가 표시됩니다.
   - 시선: 대화 상대가 있는 쪽(왼쪽/오른쪽)
   - **행동**: **✅ 검증된 동작** 버튼(손 흔들기, 박수, 어깨 으쓱, 팔짱, 가리키기, 손사래, 양손 번쩍, 주먹 불끈, 엄지 척,
     손가락 하나, 머리 긁적, 웃음 터짐, 앞으로 쑥, 깜짝 놀람)을 누르면 테스트에서 움직임이 잘 나온 자세한 문장이
     그대로 들어갑니다. 한국어로 직접 쓰면 **뜻 그대로 영어로 번역**됩니다. **🎬 AI가 행동 정하기**는 대사에 맞는
     검증된 동작을 골라 줍니다.
   - 말투(16종), 화면 속 인원, 샷 크기, 카메라 움직임
   - 프롬프트: 실제로 보내는 프롬프트를 보고 고칠 수 있고, **📜 이 컷에 예전에 쓴 프롬프트**에서 전에 만든 영상과
     프롬프트를 보고 ⭐ 표시하거나 같은 시드로 다시 쓸 수 있습니다.
5. **만들기**: 위쪽 ⏱ 상자의 **▶ 지금 남은 컷 전부 만들기**, 또는 컷마다 **이 컷 만들기 / 만들고 다음 컷 →**.
   진행 막대에 지금 하는 일과 남은 시간이 나옵니다. 다 만들어지면 마지막 확인(뒤돌아선 컷 다시 만들기)을 한 번 하고
   **자동으로 합쳐서 원본 | AI 비교 영상**을 보여 줍니다.
6. 고르는 동안 서버가 인물 사진 → 목소리 → 영상을 **미리 만들어 둡니다** (⚡ 표시). 같은 설정은 다시 만들지 않고
   저장된 것을 바로 씁니다.

### 걸리는 시간 (RTX 4070 Ti SUPER 16 GB, 고화질)

| 길이 | 기본 배경 | 새 배경 (인물 사진 새로 그림) |
|---|---|---|
| 10초 (8컷) | 약 7~9분 | 약 13분 |
| 20초 (16컷) | 약 15분 | 약 18~22분 |
| 30초 (23컷) | 약 20분 | 약 28~30분 |

영상 1컷은 약 45~55초입니다. ⚡ 빠른 렌더를 켜면 컷당 약 35초.

### 관리자 모드 (새 프리셋 만들기)

`http://127.0.0.1:8010/?admin=1` 로 열면 상단에 🔧 관리자가 생깁니다. 유튜브 주소와 시작 시간·길이를 넣으면
그 부분만 내려받아 컷으로 나누고, 컷마다 인원·구도·카메라를 자동으로 읽습니다. 다듬은 뒤 "공개"하면 첫 화면에 나옵니다.

### 가상 인물 더 만들기

`scripts/make_characters.py`의 `PEOPLE` 목록에 이름·성별·생김새·목소리를 추가하고 실행하면 Z-Image-Turbo로 얼굴 사진을
그려 인물 목록에 넣습니다 (ComfyUI가 켜져 있어야 함).

```bat
.venv\Scripts\python scripts\make_characters.py
```

---

## 6. UDP 원격 서버 / 클라이언트 (`udp/`)

다른 PC에서 **IP로 접속해** 영상 만들기를 시작하고, 진행 상황을 보고, 완성 영상을 받아옵니다. 통신은 전부 UDP입니다.

```
[클라이언트 PC]                         [서버 PC: 그래픽카드 + ComfyUI + 영상 앱]
AIVideoClient.exe  ── UDP 9999 ──▶  AIVideoServer.exe ── HTTP ──▶ 영상 앱 (127.0.0.1:8010)
```

| 파일 | 어디서 실행 | 설명 |
|---|---|---|
| `udp/release/AIVideoServer.exe` | 영상 앱이 켜진 PC | 콘솔 창. 클라이언트에 넣을 **이 PC의 IP**를 보여 주고, 접속한 IP와 명령을 기록 |
| `udp/release/AIVideoClient.exe` | 아무 Windows PC | 창 프로그램. 서버 IP·포트·비밀번호 → 연결 |

**서버**

```bat
udp\release\AIVideoServer.exe --port 9999 --password 원하는비밀번호
```

- exe 옆에 `server_config.json`을 둬도 됩니다: `{"port": 9999, "password": "...", "app": "http://127.0.0.1:8010"}`
- **비밀번호를 꼭 설정하세요.** 없으면 같은 네트워크의 누구나 렌더를 시작할 수 있습니다.
- 다른 PC에서 접속하려면 서버 PC의 Windows 방화벽에서 **UDP 9999 인바운드**를 허용해야 합니다. 처음 실행할 때 뜨는
  "액세스 허용" 창에서 *개인 네트워크*를 허용하거나, 관리자 PowerShell에서:

```powershell
New-NetFirewallRule -DisplayName "AIVideo UDP" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow
```

**클라이언트**

1. 서버 IP(서버 창에 표시), 포트(기본 9999), 비밀번호 → **연결**
2. 영상과 길이 고르고 **▶ 만들기 시작**
3. 목록에서 실행을 고르면 진행 상황(완성 컷 수, 지금 하는 일, 남은 시간)이 5초마다 갱신
4. 다 되면 자동으로 합치고 **⬇ 원본|AI 비교 영상 받기 / ⬇ AI 결과만 받기**가 켜짐
5. 받은 파일은 `다운로드\AI영상_받은파일`에 저장되고 바로 재생

**프로토콜** (`udp/common/proto.py`)

- 요청: JSON 데이터그램 `{"v":1, "id":번호, "cmd":"명령", "pw":"비밀번호", ...}` / 응답: 같은 `id`의 JSON
- 명령: `ping`, `catalog`, `runs`, `start`, `render`, `status`, `assemble`, `file_info`, `chunks`
- 파일: `AVC1` + 요청 id(uint32) + 조각 번호(uint32) + 데이터(최대 8000바이트), 빠진 조각만 다시 요청, SHA-256 확인
- 같은 id의 요청은 한 번만 실행 (재전송해도 렌더가 두 번 시작되지 않음)
- 측정: 손실 없음 2 MB 0.1초 / 패킷 20% 손실 2 MB 13초 (모두 원본과 동일),
  다른 IP 간(127.0.0.77 → 127.0.0.66) 10/20/30초 만들기~받기 전부 확인

**빌드** (소스에서): `udp\build.bat` (처음 한 번 `python -m venv udp\.buildenv` 후
`udp\.buildenv\Scripts\python -m pip install pyinstaller`)

---

## 7. ComfyUI 워크플로 (`workflows/`)

| 파일 | 용도 |
|---|---|
| `image_qwen_image_edit_2511 IMAGE.json` | 인물 사진 합성 (Qwen-Image-Edit 2511, 이미지 최대 3장) |
| `tts_voicecloneAUDIO.json` | 목소리 복제 TTS (Qwen3-TTS 1.7B, 한국어) |
| `video_ltx2_3_ia2v VIDEO LAST.json` | 사진 + 음성 → 립싱크 영상 (LTX-2.3) |

ComfyUI 화면에 끌어다 놓으면 열립니다. 앱은 같은 그래프의 API 형식(`app/workflows/compose.json`, `tts.json`,
`video.json`)을 써서 ComfyUI에 직접 보냅니다.

---

## 8. 문제 해결

| 증상 | 확인 |
|---|---|
| `/api/health`에서 comfyui false | ComfyUI가 켜졌는지, 8188~8191 중 하나가 열려 있는지. Comfy Desktop 창을 두 개 띄우지 마세요 (메모리를 나눠 써서 느려짐) |
| ollama false | `ollama serve` 실행, `ollama list`에 gemma4:12b 있는지 |
| 그림이 0.01초 만에 "성공"하고 영상이 없음 | `COMFYUI_INPUT_DIR / OUTPUT_DIR`가 실제 ComfyUI 폴더와 같은지 |
| 갑자기 컷당 2배 이상 느림 | ComfyUI를 며칠 켜 두면 느려짐 → 앱이 쉬는 동안 자동으로 다시 켬. 화면의 🐢 경고 참고 |
| 목소리가 안 나옴 | ComfyUI Python에 3-3의 패키지 버전이 맞는지 |
| 다른 PC에서 UDP 접속 안 됨 | 서버 방화벽 UDP 9999, 같은 네트워크인지, 비밀번호 |
| 프리셋 컷 사진이 안 보임 | `scripts\restore_preset_video.py` 실행 |
