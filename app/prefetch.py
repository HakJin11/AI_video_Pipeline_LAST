"""Make a run's pieces ahead of time, so pressing "만들기" mostly finds them done (2026-10-02, user: "the making
time should not be felt -- use the time spent on background prompts and so on").

While the viewer picks people, a place or acting choices, one background thread makes, in this order:
  1. every still the run's cuts need (cached per person + outfit + place + shot size),
  2. every voice line (cached per text + voice + tone),
  3. every cut clip, into the finished-clip cache (follow_render.clip_cache_key) -- not onto the cut: a cut the
     viewer then renders with the same settings is copied in at once, a cut they change is simply made anew.

All of it runs in comfy_client.background_mode(): it never starts a GPU job while the user is waiting on one (a
cut render, a background picture, a gemma prompt clean-up); the job already running is left to finish. A change
to the run restarts the pass after a short pause, so typing does not keep throwing work away.
"""
import threading
import time
import traceback

from app import comfy_client, db
from app import follow_render as fr

DEBOUNCE_SEC = 6.0  # wait this long after the last change before starting
_lock = threading.Lock()
_wake = threading.Event()
_want: dict[int, float] = {}  # project id -> time of its last change
_state: dict[int, dict] = {}  # project id -> progress for the page
_off: set[int] = set()  # runs the viewer switched it off for
_hold_until = 0.0  # the viewer is typing in a box that gemma cleans up: start no new GPU job until then
HOLD_SEC = 40.0


_last_activity = 0.0
QUIET_FOR_STILLS = 15.0
QUIET_FOR_CLIPS = 60.0


_place_stills: dict[int, int] = {}  # run -> how many "draw the people for the new place" jobs are going (or waiting)


def place_stills(pid: int, on: bool, progress: tuple[int, int] | None = None) -> None:
    """The pictures for a newly picked place are being drawn (routes/follow.py _stills_for_place): that IS the
    first step of making ahead, so the page shows it as such (it said "paused" before, 2026-10-07)."""
    with _lock:
        if on:
            if progress is None:  # a job starts (counted: picking place A then B queues a second one)
                _place_stills[pid] = _place_stills.get(pid, 0) + 1
            st = _state.setdefault(pid, {})
            st["state"] = "stills"
            if progress:
                st["stills"] = list(progress)
        elif _place_stills.get(pid, 0) > 1:
            _place_stills[pid] -= 1
        else:
            _place_stills.pop(pid, None)


def touch() -> None:
    """The viewer did something (a change, a ✨ clean-up, a click on the place panel). The pass starts no new GPU
    job until they have been quiet for a while: a video clip of the preset's own cafe, started the moment the run
    was opened, made the viewer's first ✨ wait 57 s (2026-10-06) -- and the place was changed right after anyway."""
    global _last_activity
    _last_activity = time.time()


def hold(seconds: float = HOLD_SEC) -> None:
    """The page says the viewer is writing a place / action / prompt. gemma's clean-up runs in seconds on an idle
    card but ~50 s on the CPU next to a running LTX job (2026-10-02 test), so the pass starts nothing new for a
    while; the job already running is left to finish (interrupting LTX can wedge ComfyUI)."""
    global _hold_until
    _hold_until = max(_hold_until, time.time() + seconds)
_worker: threading.Thread | None = None


def request(pid: int) -> None:
    """Something in this run changed (or it was just opened): (re)start making its pieces."""
    p = db.get_follow_project(pid)
    if not p or p.get("is_template") or pid in _off:
        return
    touch()
    with _lock:
        _want[pid] = time.time()
        # the counts of the last pass would read as done; keep only the clips already made (checked in status())
        _state[pid] = {"state": "waiting", "ready": (_state.get(pid) or {}).get("ready", {})}
    _ensure_worker()
    _wake.set()


def resume(pid: int) -> None:
    """The page was opened: carry on if nothing is going for this run (e.g. after a restart), but do not restart a
    pass that is already running -- that threw away the clip being made (2026-10-02 test)."""
    st = (_state.get(pid) or {}).get("state")
    if pid in _want or st in ("stills", "voices", "clips", "paused"):
        return
    request(pid)


def set_enabled(pid: int, on: bool) -> None:
    if on:
        _off.discard(pid)
        request(pid)
    else:
        _off.add(pid)
        with _lock:
            _want.pop(pid, None)
            _state[pid] = {**_state.get(pid, {}), "state": "off"}


def project_signature(pid: int) -> tuple:
    p = db.get_follow_project(pid) or {}
    cast = tuple((m["id"], m.get("character_id"), m.get("voice_id"), m.get("costume"))
                 for m in db.list_follow_cast(pid))
    return (p.get("setting"), p.get("background_id"), p.get("use_still"), p.get("fast_preview"),
            p.get("match_reference"), cast)


def status(pid: int) -> dict:
    """For the page: what is being made and which cuts are ready (made with the run's current settings)."""
    st = dict(_state.get(pid) or {})
    ready = st.pop("ready", {})
    psig = project_signature(pid)
    ok = []
    for c in db.list_follow_cuts(pid):
        r = ready.get(c["id"])
        if r and r[0] == fr.cut_signature(c) and r[1] == psig and fr.cached_clip(r[2]):
            ok.append(c["id"])
    st["ready_cuts"] = ok
    st["enabled"] = pid not in _off
    return st


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, daemon=True, name="prefetch")
        _worker.start()


def _foreground_render() -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM follow_cuts WHERE status IN ('queued', 'rendering') LIMIT 1").fetchone() \
            is not None


comfy_client.background_should_wait = lambda: _foreground_render()


def _user_busy() -> bool:
    """The viewer is editing (a ✨ clean-up in the last 20 s, or typing in a box ✨ cleans up). Not once they pressed
    make: then the stills are what they wait for."""
    now = time.time()
    return (now - _last_activity < 20 or now < _hold_until) and not _foreground_render()


fr.user_busy = _user_busy


class _Changed(Exception):
    pass


def _loop() -> None:
    with comfy_client.background_mode():
        while True:
            _wake.wait(15)
            _wake.clear()
            while True:
                with _lock:
                    now = time.time()
                    due = [(t, pid) for pid, t in _want.items() if now - t >= DEBOUNCE_SEC]
                    waiting = bool(_want)
                if not due:
                    if waiting:
                        time.sleep(1)
                        continue
                    break
                stamp, pid = max(due)  # the run touched last first
                try:
                    _work(pid, stamp)
                    with _lock:
                        if _want.get(pid) == stamp:
                            _want.pop(pid, None)
                            _state[pid]["state"] = "done"
                except _Changed:
                    continue  # changed again: start over after the pause
                except Exception as exc:  # noqa: BLE001 - background work must never take the app down
                    traceback.print_exc()
                    with _lock:
                        if _want.get(pid) == stamp:
                            _want.pop(pid, None)
                        _state.setdefault(pid, {}).update(state="error", error=str(exc)[:200])


def _work(pid: int, stamp: float) -> None:
    def check(quiet: float = QUIET_FOR_STILLS) -> None:
        """Before every step: the run still exists and was not changed, nobody is waiting on the GPU, and the
        viewer has been quiet for `quiet` seconds."""
        while True:
            if _want.get(pid) != stamp or pid in _off or not db.get_follow_project(pid):
                raise _Changed()
            if (not _foreground_render() and not comfy_client.foreground_busy() and time.time() >= _hold_until
                    and time.time() - _last_activity >= quiet):
                return
            _set(state="stills" if pid in _place_stills else "paused")
            time.sleep(2)

    def _set(**kw) -> None:
        with _lock:
            _state.setdefault(pid, {}).update(kw)

    project = db.get_follow_project(pid)
    cuts = [c for c in db.list_follow_cuts(pid) if c["cast_id"]]
    psig = project_signature(pid)

    # 1. stills: one per person / pair + shot size (+ place)
    check()
    _set(state="stills")
    fr.prewarm_stills(pid, quiet=True, progress=lambda i, n: (check(), _set(stills=[i, n])))
    n_st = len({(c["cast_id"], c.get("cast_id2") if (c.get("people") or 1) >= 2 else None, c.get("framing") or "medium")
                for c in cuts if not c["clip_path"]})
    _set(stills=[n_st, n_st])

    # 2. voices, all lines in one TTS load
    check()
    _set(state="voices")
    fr.prewarm_voices(pid, quiet=True, progress=lambda i, n: _set(voices=[i, n]))
    n_lines = sum(1 for c in cuts if (c["line"] or "").strip())
    _set(voices=[n_lines, n_lines])

    # 3. clips into the cache, in cut order
    todo = [c for c in db.list_follow_cuts(pid) if c["cast_id"] and not c["clip_path"]
            and c["status"] not in ("queued", "rendering")]
    ready = dict(_state.get(pid, {}).get("ready", {}))
    for i, c in enumerate(todo):
        check(QUIET_FOR_CLIPS)
        _set(state="clips", clips=[i, len(todo)], current=c["position"] + 1)
        job = fr._prepare_cut(c["id"])
        try:
            key = fr.clip_cache_key(job)
            if not fr.cached_clip(key):
                check(QUIET_FOR_CLIPS)  # making the voice/still above may have taken a while
                fr.making(key, True)
                try:
                    fr.run_ltx_checked([job], fast=job["fast"])
                    tmp = job["dest"].with_name(f"prefetch_{c['id']}_{key[:8]}.mp4")
                    fr._encode_cut(job["dest"], job["clip_len"], tmp, job.get("voice"))
                    fr._store_clip(key, tmp, job["clip_len"])
                    tmp.unlink(missing_ok=True)
                finally:
                    fr.making(key, False)
            ready[c["id"]] = (fr.cut_signature(job["cut"]), psig, key)
            _set(ready=dict(ready))
        finally:
            fr._drop_job_files(job)
    _set(clips=[len(todo), len(todo)], current=None)
