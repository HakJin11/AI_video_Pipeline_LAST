"""Hear back what a voice take actually says (Whisper), to catch broken takes before they reach a video.

2026-10-01 voice test: a tone reference clip ending in "...힘들어" made the line come out "힘들어. 아 진짜 딱 한
판만 하자", and garbled takes happened too. The app's venv has no Whisper, so a helper process runs with
ComfyUI's Python (CPU, model 'small', loaded once, ~1-2 s per short take)."""
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from app.config import BASE_DIR, FFMPEG_PATH

# ComfyUI's Python (it has torch + openai-whisper). Another install: set COMFYUI_PYTHON.
_PY = Path(os.environ.get("COMFYUI_PYTHON", str(BASE_DIR / "ComfyUI (1)" / "ComfyUI" / ".venv" / "Scripts" / "python.exe")))
_WORKER = Path(__file__).with_name("stt_worker.py")
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_broken = False


def _start() -> subprocess.Popen | None:
    global _proc, _broken
    if _proc and _proc.poll() is None:
        return _proc
    if _broken or not _PY.exists():
        return None
    env = dict(os.environ, PATH=str(Path(FFMPEG_PATH).parent) + os.pathsep + os.environ.get("PATH", ""),
               PYTHONIOENCODING="utf-8")
    try:
        _proc = subprocess.Popen([str(_PY), str(_WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = json.loads(_proc.stdout.readline() or "{}")
        if not ready.get("ready"):
            raise RuntimeError("worker did not start")
        return _proc
    except Exception:  # noqa: BLE001 - no checker: takes are used unchecked, as before
        _broken = True
        _proc = None
        return None


def transcribe(path: Path) -> str | None:
    """What the take says, or None when the checker is unavailable."""
    with _lock:
        proc = _start()
        if not proc:
            return None
        try:
            proc.stdin.write(str(path) + "\n")
            proc.stdin.flush()
            out = json.loads(proc.stdout.readline() or "{}")
        except Exception:  # noqa: BLE001
            return None
        return out.get("text")


def _core(text: str) -> str:
    return re.sub(r"[^가-힣a-zA-Z0-9]", "", text or "")


_NUM_WORDS = re.compile(r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스물|하나|둘|셋|넷|일|이|삼|사|오|육|칠|팔|구|십|백)"
                        r"(?=\s*(시간|번|명|개|살|시|분|판|잔|층|월|일|원|대|등|위|장|권|마리|병)|$)")


def mismatch(wanted: str, heard: str | None) -> float:
    """Character error rate of what was heard against what was wanted (Hangul/letters/digits only).
    Whisper writes counted numbers as digits ("한 시간" -> "1시간", "네 명" -> "4명"): when the heard text
    has digits, number words before a counter are dropped from the wanted text and digits from the heard."""
    if heard is None:
        return 0.0
    wanted = re.sub(r"^\[v\d+\]\s*", "", wanted or "")
    if re.search(r"\d", heard + wanted):  # digits on either side: compare without counted numbers
        wanted, heard = (re.sub(r"\d", "", _NUM_WORDS.sub("", t)) for t in (wanted, heard))
    a, b = _core(wanted), _core(heard)
    if not a:
        return 0.0
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[len(b)] / len(a)
