# AI 영상 파이프라인 — UDP 서버 / 클라이언트

"유튜브 따라하기" AI 영상 앱(ComfyUI + LTX-2.3 + Qwen3-TTS + gemma)을 **다른 PC에서 IP로 접속해** 조종하고
완성 영상을 받아오는 프로그램입니다. 통신은 전부 **UDP**입니다.

```
[클라이언트 PC]                         [서버 PC: 그래픽카드 + ComfyUI + 영상 앱]
AIVideoClient.exe  ── UDP 9999 ──▶  AIVideoServer.exe ── HTTP ──▶ 영상 앱 (127.0.0.1:8010)
  영상 고르기·만들기 시작                명령을 앱 API로 전달
  진행 상황·남은 시간                    완성 영상을 8KB 조각으로 UDP 전송
  완성 영상 받기 (조각 재요청·SHA-256 확인)
```

## 실행 파일

| 파일 | 어디서 실행 | 설명 |
|---|---|---|
| `release/AIVideoServer.exe` | 영상 앱이 켜져 있는 PC | 콘솔 창. 시작하면 클라이언트에 넣을 **이 PC의 IP**를 보여 줍니다 |
| `release/AIVideoClient.exe` | 아무 Windows PC | 창 프로그램. 서버 IP·포트·비밀번호 입력 → 연결 |

### 서버

```bat
AIVideoServer.exe --port 9999 --password 원하는비밀번호
```

- 옵션 대신 exe 옆에 `server_config.json`을 둬도 됩니다: `{"port": 9999, "password": "...", "app": "http://127.0.0.1:8010"}`
- **비밀번호를 꼭 설정하세요.** 비밀번호가 없으면 같은 네트워크의 누구나 렌더를 시작할 수 있습니다.
- 다른 PC에서 접속하려면 Windows 방화벽에서 **UDP 9999 인바운드**를 허용해야 합니다
  (처음 실행할 때 뜨는 "액세스 허용" 창에서 허용하거나, 관리자 PowerShell에서
  `New-NetFirewallRule -DisplayName "AIVideo UDP" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow`).
- 영상 앱(FastAPI, 포트 8010)과 ComfyUI가 먼저 켜져 있어야 합니다.

### 클라이언트

1. 서버 IP(서버 창에 표시), 포트(기본 9999), 비밀번호 입력 → **연결**
2. 영상과 길이(10/20/30초) 고르고 **▶ 만들기 시작**
3. 목록에서 실행을 고르면 진행 상황(완성 컷 수, 지금 하는 일, 남은 시간)이 5초마다 갱신
4. 모든 컷이 끝나면 자동으로 합치고 **⬇ 원본|AI 비교 영상 받기** / **⬇ AI 결과만 받기** 버튼이 켜집니다
5. 받은 파일은 `다운로드\AI영상_받은파일`에 저장되고 바로 재생됩니다

입력한 IP·포트·비밀번호는 `%APPDATA%\AIVideoClient\config.json`에 기억됩니다.

## UDP 프로토콜 (`common/proto.py`)

- 요청: JSON 데이터그램 `{"v":1, "id":번호, "cmd":"명령", "pw":"비밀번호", ...}`
- 응답: 같은 `id`의 JSON `{"id":번호, "ok":true, ...}` 또는 `{"ok":false, "error":"..."}`
- 명령: `ping`, `catalog`, `runs`, `start`(template_id, length_sec), `render`(run_id), `status`(run_id),
  `assemble`(run_id), `file_info`(run_id, kind), `chunks`(run_id, kind, idx[])
- 파일: `AVC1` + 요청 id(uint32) + 조각 번호(uint32) + 데이터(최대 8000바이트)
- 신뢰성: 응답이 없으면 같은 id로 다시 보냄. 서버는 같은 id의 요청을 한 번만 실행하고(진행 중인 재전송은 무시,
  끝난 것은 저장된 답을 다시 보냄) 렌더가 두 번 시작되지 않습니다. 영상은 빠진 조각만 다시 요청하고,
  손실이 많으면 한 번에 요청하는 조각 수를 줄입니다. 마지막에 SHA-256으로 원본과 같은지 확인합니다.
- 측정(같은 PC): 손실 없음 2 MB 0.1초 / 패킷 20% 손실 2 MB 13초, 두 경우 모두 원본과 바이트 단위로 동일.
- 다른 IP 간 테스트: 클라이언트 127.0.0.77 → 서버 127.0.0.66:9999 로 10/20/30초 만들기 시작 → 진행 상황 → 합치기 →
  영상 받기까지 전부 UDP로 확인. 틀린 비밀번호(127.0.0.88)는 거절. 서버 창에 접속한 IP와 명령이 찍힙니다.
- 여러 네트워크 카드가 있는 PC는 `UdpClient(..., bind_ip="보낼 IP")`로 보내는 IP를 고를 수 있습니다.

## 소스에서 실행 / 빌드

```bat
python server\udp_server.py --password 1234
python client\udp_client.py

python -m venv .buildenv
.buildenv\Scripts\python -m pip install pyinstaller
build.bat
```

Python 3.11+ 표준 라이브러리만 씁니다 (클라이언트 창은 tkinter).

## ComfyUI 워크플로 (`workflows/`)

| 파일 | 용도 | 주요 모델 |
|---|---|---|
| `image_qwen_image_edit_2511 IMAGE.json` | 인물 사진 합성 (Qwen-Image-Edit 2511, 이미지 최대 3장 입력) | qwen_image_edit_2511_fp8mixed, qwen_2.5_vl_7b_fp8_scaled, Lightning 4steps LoRA |
| `tts_voicecloneAUDIO.json` | 목소리 복제 TTS (Qwen3-TTS 1.7B, 한국어) | Qwen3-TTS-12Hz-1.7B-Base |
| `video_ltx2_3_ia2v VIDEO LAST.json` | 사진 + 음성 → 립싱크 영상 (LTX-2.3 image+audio to video) | ltx-2.3-22b-dev-fp8, distilled LoRA, gemma_3_12B_it_fp4_mixed, spatial upscaler x2 |

ComfyUI에서 파일을 끌어다 놓으면 열리고, 없는 모델은 Manager의 "Install Missing Models"로 받을 수 있습니다.
