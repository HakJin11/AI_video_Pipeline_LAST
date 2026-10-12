# AGENTS.md — server (for AI coding agents: Claude Code, Codex, Cursor ...)

You are setting up and running the **server side** of the AI video pipeline on a Windows PC with an NVIDIA GPU
(16 GB VRAM, 32 GB RAM tested). The human-facing guide is `README.md` (Korean); follow it, and use this file as the
checklist. Do each step, verify it, then go on. Ask the user before large downloads (models ~100 GB) and before
anything that needs admin rights (firewall rule).

## What runs here
- `app/` FastAPI web app on `127.0.0.1:8010` (UI in `frontend/`, data in `data/app.db` + `media/`)
- ComfyUI (port 8188, or 8189-8191) — renders stills (Qwen-Image-Edit), voices (Qwen3-TTS), video (LTX-2.3)
- Ollama `gemma4:12b` on `127.0.0.1:11434` — prompt clean-up / translation; must run on the GPU (never num_gpu 0)
- ffmpeg — cutting / joining
- `udp_server/` — UDP server on port 9999 that clients use (commands + full web-screen relay)

## Steps
1. **ffmpeg**: `C:\ffmpeg\bin\ffmpeg.exe` exists, or `ffmpeg` on PATH, or set `FFMPEG_PATH`/`FFPROBE_PATH`.
   Verify: `ffmpeg -version`.
2. **Ollama**: installed and `ollama pull gemma4:12b`. Verify: `curl http://127.0.0.1:11434/api/tags` lists gemma4:12b.
3. **ComfyUI Desktop** with custom nodes `ComfyUI-LTXVideo`, `ComfyUI-QwenTTS`, `ComfyUI-KJNodes`,
   `ComfyUI-VideoHelperSuite`, `rgthree-comfy`, `comfyui-ollama` and the models listed in README §3-3 (exact file
   names matter: `app/workflows/*.json` reference them). In ComfyUI's own venv:
   `pip install "transformers==4.57.6" "diffusers==0.35.2" "huggingface-hub==0.36.2" "kornia==0.8.2" sox onnxruntime openai-whisper`.
   Verify: `curl http://127.0.0.1:8188/queue` answers, and `curl http://127.0.0.1:8188/object_info/LTXVImgToVideoInplace`
   and `/object_info/AILab_Qwen3TTSVoiceClone_Advanced` are not empty.
4. **Paths**: if ComfyUI is not Comfy Desktop's default, set env vars `COMFYUI_INPUT_DIR`, `COMFYUI_OUTPUT_DIR`
   (its input/output folders), `COMFYUI_PYTHON` (its venv python, for Whisper), `COMFYUI_DIR` (its folder, used by the
   watchdog). Defaults are in `app/config.py`.
5. **App venv** (in this folder): `python -m venv .venv` then `.venv\Scripts\python -m pip install -r requirements.txt`.
6. **Preset video**: `.venv\Scripts\python scripts\restore_preset_video.py` (downloads the YouTube section with yt-dlp
   and cuts it; the original clip is not in the repo).
7. **Run the app**: `start_app.bat` (watchdog keeps app + ComfyUI alive; log `app_server.log`) or for a quick test
   `.venv\Scripts\python -m uvicorn app.main:app --port 8010 --host 127.0.0.1`.
   Verify: `curl http://127.0.0.1:8010/api/health` → `{"comfyui":true,"ollama":true}` and
   `curl http://127.0.0.1:8010/api/follow/catalog` lists "썸사친에 보드게임".
8. **UDP server**: copy `udp_server/server_config.example.json` to `udp_server/server_config.json`, set a password
   (ask the user), run `udp_server\start_udp_server.bat`. It prints this PC's IP for the clients.
   Firewall: inbound UDP 9999 must be allowed — ask the user to approve the Windows prompt or run (admin):
   `New-NetFirewallRule -DisplayName "AIVideo UDP" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow`.
9. Tell the user the IP, port and password to give to clients (`../client/`).

## Rules when changing code
- Restarting the app kills renders in progress: check `data/app.db` for `follow_cuts.status IN ('queued','rendering')` first.
- Every gemma call goes through `ollama_client.gemma_post` / `_refine_call` (waits for the GPU between ComfyUI jobs).
- Korean text the user types is translated literally (`/api/translate`), not reworded.
- Do not add real people's faces or recorded voices to the shared data.
- `frontend/index.html` loads `follow.js?v=N`: bump N after editing JS so browsers reload it.
