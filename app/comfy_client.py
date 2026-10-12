import copy
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from app.config import (
    COMFYUI_INPUT_DIR,
    COMFYUI_OUTPUT_DIR,
    COMFYUI_PORTS,
    COMFYUI_URL,
    FFPROBE_PATH,
    OLLAMA_URL,
)

# No read timeout: while ComfyUI is mid-render its event loop is blocked by torch for tens of
# seconds (a big video's model-init alone ran ~12 min) and can't answer /history polls -- that's
# exactly when we must keep waiting, not abandon the job as "timed out" while it's still rendering.
# wait_for_outputs() enforces the real overall deadline instead.
_client = httpx.Client(base_url=COMFYUI_URL, timeout=httpx.Timeout(30.0, read=None))
_url_lock = threading.Lock()


def base_url() -> str:
    """The URL ComfyUI is actually answering on, re-probed whenever it stops answering."""
    return str(_client.base_url).rstrip("/")


def resolve_url(force: bool = False) -> str | None:
    """Probe the candidate ports and point the client at whichever one answers.

    Comfy Desktop picks the next free port if 8188 is in use, so a restart can silently move it
    and every render then fails with "connection refused" against the old port."""
    with _url_lock:
        current = base_url()
        if not force:
            try:
                if httpx.get(f"{current}/queue", timeout=2).status_code == 200:
                    return current
            except httpx.HTTPError:
                pass
        for port in COMFYUI_PORTS:
            url = f"http://127.0.0.1:{port}"
            try:
                if httpx.get(f"{url}/queue", timeout=2).status_code == 200:
                    if url != current:
                        _client.base_url = url
                    return url
            except httpx.HTTPError:
                continue
        return None

# ComfyUI has one GPU and processes its queue serially, but our FastAPI routes can call in from
# multiple threads at once (e.g. the two per-character TTS calls fired together). Submitting two
# prompts back-to-back without waiting risks interleaving with in-node state like
# unload_models, degrading whichever job wasn't first. Serialize every submit+wait cycle through
# this app so ComfyUI only ever works on one of our jobs at a time.
_comfy_lock = threading.Lock()

# Foreground first (2026-10-02, "the user should not feel the making time"): the page now makes stills, voices
# and clips ahead of time in a background thread (app/prefetch.py). That work runs in background mode and
# never takes the GPU while the user is waiting for something -- a cut render, a background picture, a gemma
# prompt clean-up. The job already running is never interrupted (interrupting LTX wedged ComfyUI before).
_prio = threading.Condition()
_fg_waiting = 0  # foreground callers queued for _comfy_lock right now
_fg_hold = 0  # foreground activities in progress that may need the GPU next (see foreground())
_local = threading.local()


def is_background() -> bool:
    return bool(getattr(_local, "background", False))


class background_mode:
    """`with background_mode():` -- every ComfyUI job of this thread waits for foreground work."""

    def __enter__(self):
        _local.background = True

    def __exit__(self, *exc):
        _local.background = False


class foreground:
    """`with foreground():` -- the user is waiting on this; background work starts no new GPU job meanwhile."""

    def __enter__(self):
        global _fg_hold
        with _prio:
            _fg_hold += 1

    def __exit__(self, *exc):
        global _fg_hold
        with _prio:
            _fg_hold -= 1
            _prio.notify_all()


class gpu_turn:
    """`with gpu_turn():` -- take the GPU between two ComfyUI jobs for something else (gemma for a clean-up the user
    waits on). Running gemma on the CPU next to LTX instead made both crawl: LTX steps 6 s -> 16-23 s, a cut ran
    past its time budget and failed (2026-10-05, run 176)."""

    def __enter__(self):
        global _fg_waiting
        with _prio:
            _fg_waiting += 1
        try:
            _comfy_lock.acquire()
        finally:
            with _prio:
                _fg_waiting -= 1
                _prio.notify_all()

    def __exit__(self, *exc):
        _comfy_lock.release()


# set by app/prefetch.py: True while a render the user asked for is queued or running. Background jobs wait for the
# whole render, not only for its single GPU jobs -- between two of them the ahead-of-time pass slipped in and drew
# the same stills a second time (20 s run, 2026-10-06).
background_should_wait = None


def foreground_busy() -> bool:
    if _fg_waiting > 0 or _fg_hold > 0:
        return True
    if is_background() and background_should_wait is not None:
        try:
            return bool(background_should_wait())
        except Exception:  # noqa: BLE001
            return False
    return False


def wait_for_foreground(poll: float = 1.0) -> None:
    """Background threads: block until no foreground work is waiting or in progress."""
    with _prio:
        while foreground_busy():
            _prio.wait(poll)


class ComfyError(RuntimeError):
    pass


class ComfyWorkerDead(ComfyError):
    """ComfyUI's job thread crashed: the server still answers, but no job will ever run again."""


def _worker_crash_since(since: float = 0.0) -> str | None:
    """The error that killed ComfyUI's prompt worker after `since` (epoch seconds), if any. The log
    buffer only holds the running ComfyUI's lines and a dead worker never comes back, so any crash
    in it (since=0) means no job will run until ComfyUI restarts.

    2026-09-30 (and 09-21): under RAM pressure ComfyUI's memory manager raised "HostBuffer.truncate
    failed" inside the prompt_worker thread. The thread died, /queue kept showing the job as running,
    and every render waited out its whole budget (15 min) for nothing."""
    try:
        entries = _client.get("/internal/logs/raw", timeout=5).json().get("entries", [])
    except (httpx.HTTPError, ValueError):
        return None
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since))
    text = "".join(e.get("m", "") for e in entries if e.get("t", "") >= since_iso)
    if "(prompt_worker)" not in text or "Exception in thread" not in text:
        return None
    last = [l.strip() for l in text.splitlines() if l.strip()]
    return next((l for l in reversed(last) if "Error" in l), "prompt_worker crashed")


def restart_comfy(wait: float = 240.0) -> bool:
    """Restart ComfyUI through its Manager (Comfy Desktop brings the backend back by itself, ~20s)."""
    try:
        _client.post("/v2/manager/reboot", timeout=10)
    except httpx.HTTPError:
        pass  # the connection drops when it goes down: expected
    deadline = time.time() + wait
    time.sleep(5)
    while time.time() < deadline:
        url = resolve_url(force=True)
        if url:
            try:
                q = httpx.get(f"{url}/queue", timeout=3).json()
                if not q.get("queue_running"):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
        time.sleep(3)
    # 2026-10-01 23:19: after a CUDA out-of-memory the reboot never came back -- the old process kept port 8188
    # open and answered nothing, and the watchdog only starts a backend when none is running. Kill it; the
    # watchdog starts a fresh one (~4 min).
    _kill_comfy_backend()
    return _wait_for_comfy()


def _kill_comfy_backend() -> None:
    """Stop the ComfyUI backend process(es) -- python main.py with a --port, never the app's uvicorn (both run on
    the same python build, so matching by path would kill the app too)."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
              "$_.CommandLine -like '*ComfyUI*main.py*' -and $_.CommandLine -notlike '*uvicorn*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def audio_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def copy_to_comfy_input(src_path: Path) -> str:
    """Copy a local file into ComfyUI's input folder under a unique name, return that filename."""
    unique_name = f"{uuid.uuid4().hex}{src_path.suffix.lower()}"
    dest = COMFYUI_INPUT_DIR / unique_name
    shutil.copyfile(src_path, dest)
    return unique_name


def queue_prompt(graph: dict) -> str:
    payload = {"prompt": copy.deepcopy(graph), "client_id": uuid.uuid4().hex}
    resp = _client.post("/prompt", json=payload)
    if resp.status_code != 200:
        raise ComfyError(f"ComfyUI /prompt failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    if "prompt_id" not in data:
        raise ComfyError(f"ComfyUI /prompt did not return prompt_id: {data}")
    return data["prompt_id"]


def wait_for_outputs(prompt_id: str, timeout: float = 300.0, poll_interval: float = 2.0) -> dict:
    """Poll /history/{prompt_id} until the job finishes. Returns the outputs mapping {node_id: {...}}."""
    started = time.time()
    deadline = started + timeout
    next_check = started + 15
    while time.time() < deadline:
        if time.time() >= next_check:
            next_check = time.time() + 15
            crash = _worker_crash_since()
            if crash:
                raise ComfyWorkerDead(f"ComfyUI 작업 스레드가 멈췄습니다 ({crash})")
        resp = _client.get(f"/history/{prompt_id}")
        if resp.status_code == 200:
            history = resp.json()
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msg = next((m[1].get("exception_message", "") for m in status.get("messages", [])
                                if m and m[0] == "execution_error"), "")
                    raise ComfyError(f"ComfyUI job {prompt_id} failed: {msg or status}")
                outputs = entry.get("outputs")
                if outputs:
                    return outputs
        time.sleep(poll_interval)
    # give the GPU back: a job that ran past its budget (seen after an out-of-memory) otherwise kept
    # ComfyUI busy for hours and every later cut queued behind it
    try:
        _client.post("/interrupt", json={"prompt_id": prompt_id}, timeout=5)
        _client.post("/queue", json={"delete": [prompt_id]}, timeout=5)
    except httpx.HTTPError:
        pass
    raise ComfyError(f"ComfyUI job {prompt_id} timed out after {timeout}s")


def free_gpu_from_ollama() -> None:
    """Unload gemma before any ComfyUI job. ollama_client keeps it resident between script calls
    while ComfyUI is idle (each reload cost 4-16s); it must never share the 16GB card with
    LTX/TTS/Qwen -- on 2026-09-15 a render and a gemma call together hung the gemma call 3 min."""
    if is_background() and foreground_busy():
        return  # the user's own gemma call (prompt clean-up) may be using it right now
    try:
        for _ in range(6):  # a model busy answering cannot be unloaded: do not spin on it for 10+ s
            loaded = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=5).json().get("models") or []
            if not loaded:
                return
            for m in loaded:
                httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=30)
            time.sleep(0.5)
    except (httpx.HTTPError, ValueError):
        pass  # Ollama down or busy: nothing we can free, let the job run


def free_vram(force: bool = False) -> None:
    """Ask ComfyUI to drop its loaded models so gemma can run on the card. With ~13GB held by ComfyUI,
    Ollama put gemma entirely on the CPU and every line/translation took 30s+ (2026-09-22: a 10s run
    sat 11 minutes before its first clip). ComfyUI reloads what it needs on the next job."""
    if not force and is_background() and (foreground_busy() or _comfy_lock.locked()):
        return  # never pull the models out from under a job the user is waiting on
    # force: the caller holds the ComfyUI lock itself (ollama_client.gemma_turn), so no job is running
    try:
        httpx.post(f"{base_url()}/free", json={"unload_models": True, "free_memory": True}, timeout=10)
    except httpx.HTTPError:
        return
    time.sleep(3)  # ComfyUI frees between queue items, not instantly
    # a gemma already loaded while the card was full stays on the CPU even after it frees up
    free_gpu_from_ollama()


def _wait_for_comfy(max_wait: float = 600.0) -> bool:
    """ComfyUI is gone (crashed or restarting): the watchdog brings it back within a few minutes. Waiting
    here beats failing: on 2026-09-30 22:31 ComfyUI died of a memory error and cuts 2-6 of a 20s run each
    failed at once with "cannot connect" during the 6 minutes it took to come back."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if resolve_url(force=True) is not None:
            return True
        time.sleep(5)
    return False


def run_prompt_and_wait(graph: dict, timeout: float = 300.0, poll_interval: float = 2.0) -> dict:
    """Queue a graph and wait for its outputs, holding the ComfyUI lock for the whole cycle so no
    other job from this app can be submitted in between."""
    global _fg_waiting
    background = is_background()
    if background:
        while True:
            wait_for_foreground()
            _comfy_lock.acquire()
            if not foreground_busy():
                break
            _comfy_lock.release()  # someone came in between: let them go first
            time.sleep(0.5)
    else:
        with _prio:
            _fg_waiting += 1
        try:
            _comfy_lock.acquire()
        finally:
            with _prio:
                _fg_waiting -= 1
                _prio.notify_all()
    try:
        return _run_locked(graph, timeout, poll_interval)
    finally:
        _comfy_lock.release()


def _run_locked(graph: dict, timeout: float, poll_interval: float) -> dict:
    if resolve_url() is None and not _wait_for_comfy():
        raise ComfyError("ComfyUI에 연결할 수 없습니다 (8188~8191 모두 응답 없음). ComfyUI를 켜 주세요.")
    free_gpu_from_ollama()
    crash = _worker_crash_since()
    if crash and not restart_comfy():  # died earlier (another job, or someone using ComfyUI directly)
        raise ComfyError(f"ComfyUI 작업 스레드가 멈춰 있습니다 ({crash}). ComfyUI를 재시작해 주세요.")
    try:
        prompt_id = queue_prompt(graph)
        return wait_for_outputs(prompt_id, timeout=timeout, poll_interval=poll_interval)
    except ComfyWorkerDead as dead:
        # restart ComfyUI and give the job one more go: the input files are still in its input folder
        if not restart_comfy():
            raise ComfyError(f"{dead} -- ComfyUI를 다시 켜지 못했습니다. ComfyUI를 재시작해 주세요.") from dead
    except httpx.HTTPError as gone:
        # the connection dropped mid-job: the whole ComfyUI process went down. Wait for it and resend.
        if not _wait_for_comfy():
            raise ComfyError("ComfyUI가 작업 중에 꺼졌고 다시 켜지지 않았습니다. ComfyUI를 켜 주세요.") from gone
    prompt_id = queue_prompt(graph)
    return wait_for_outputs(prompt_id, timeout=timeout, poll_interval=poll_interval)


def _find_output_file(filename: str, subfolder: str) -> Path:
    candidate = COMFYUI_OUTPUT_DIR / subfolder / filename if subfolder else COMFYUI_OUTPUT_DIR / filename
    if candidate.exists():
        return candidate
    matches = list(COMFYUI_OUTPUT_DIR.rglob(filename))
    if matches:
        return matches[0]
    raise ComfyError(f"Could not locate ComfyUI output file: {filename} (subfolder={subfolder})")


def collect_first_file(outputs: dict, media_keys: str | list[str]) -> Path:
    """Find the first output file matching any of media_keys (e.g. 'images', 'audio', 'video(s)')."""
    keys = [media_keys] if isinstance(media_keys, str) else media_keys
    for node_output in outputs.values():
        for key in keys:
            files = node_output.get(key)
            if files:
                f = files[0]
                return _find_output_file(f["filename"], f.get("subfolder", ""))
    raise ComfyError(f"No output found for keys {keys} in ComfyUI result: {outputs}")


def save_output_to(outputs: dict, dest_path: Path, media_keys: str | list[str] = "images") -> Path:
    src = collect_first_file(outputs, media_keys)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest_path)
    return dest_path
