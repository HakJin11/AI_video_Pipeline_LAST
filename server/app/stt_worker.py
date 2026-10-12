"""Whisper helper, run with ComfyUI's Python (it has openai-whisper; the app's venv does not).
Reads one audio path per line on stdin, writes {"text": ...} per line on stdout. CPU, model 'small'."""
import json
import sys
import warnings

warnings.filterwarnings("ignore")
import whisper  # noqa: E402

model = whisper.load_model("small", device="cpu")
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    path = line.strip()
    if not path:
        continue
    try:
        text = model.transcribe(path, language="ko", fp16=False)["text"].strip()
        print(json.dumps({"text": text}, ensure_ascii=False), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)[:200]}), flush=True)
