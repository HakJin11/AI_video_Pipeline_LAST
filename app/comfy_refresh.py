"""Restart ComfyUI by itself when it has gone slow (2026-10-06).

After running for days, the video sampler crawled: LTX stage-1 steps took 5.5 s instead of 1.5 s and a 1.3 s cut
took 110 s instead of 60 s, with only ~1.5 GB of the 32 GB RAM left (ComfyUI held 36 GB of private memory).
A fresh ComfyUI was back to full speed at once. So while nothing is being made, this checks the speed of the last
video job in ComfyUI's own log and restarts it when the steps got slow. (Its private memory is no guide: a
fresh ComfyUI holds ~31 GB after one video.)
"""
import re
import subprocess
import threading
import time

import httpx

from app import comfy_client, db

CHECK_EVERY_SEC = 300
SLOW_STEP_SEC = 3.0  # LTX stage 1 (8 steps): ~1.5 s/step fresh, 4.5-6 s/step when it had gone slow
_last_restart = 0.0
_state = {"last_check": None, "last_step_sec": None, "private_gb": None, "restarts": 0, "last_reason": None}


def status() -> dict:
    return dict(_state)


def _last_stage1_step_sec() -> float | None:
    try:
        entries = httpx.get(f"{comfy_client.base_url()}/internal/logs/raw", timeout=5).json().get("entries", [])
    except (httpx.HTTPError, ValueError):
        return None
    text = "".join(e.get("m", "") for e in entries)
    # stage 1 = its own step count (8, now 5 since 2026-10-06 -- an "8/8" match never fired after that); the last
    # finished bar tells the current speed. Stills are 4/4 bars, stage 2 is 3/3.
    from app.scene_render import _speed_tuning
    t = _speed_tuning()
    if t.get("ltx_sigmas1"):
        n = len([x for x in str(t["ltx_sigmas1"]).split(",") if x.strip()]) - 1
    else:
        n = int(t.get("ltx_stage1_steps") or 8)
    hits = re.findall(rf"{n}/{n} \[[0-9:]+<[0-9:]+,\s+([0-9.]+)s/it\]", text)
    # the faster of the last two video jobs: one slow job is usually something else using the PC at the same
    # time (gemma on the CPU next to a remade cut, 121 s, restarted a healthy ComfyUI; 2026-10-06), a ComfyUI that
    # has gone slow is slow every time
    return min(float(x) for x in hits[-2:]) if len(hits) >= 2 else None


def _private_gb() -> float | None:
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
              "$_.CommandLine -like '*ComfyUI*main.py*' -and $_.CommandLine -notlike '*uvicorn*' } | "
              "Measure-Object -Property PrivatePageCount -Sum | ForEach-Object { $_.Sum }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        return float(out) / 2 ** 30 if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _idle() -> bool:
    """Nothing made or waited for: no cut queued/rendering, ComfyUI's queue empty, no job holding the GPU."""
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM follow_cuts WHERE status IN ('queued', 'rendering') LIMIT 1").fetchone():
            return False
    if comfy_client._comfy_lock.locked() or comfy_client.foreground_busy():
        return False
    try:
        q = httpx.get(f"{comfy_client.base_url()}/queue", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return False
    return not (q.get("queue_running") or q.get("queue_pending"))


def check_once() -> str | None:
    """Restart ComfyUI if it is idle and slow. Returns the reason when it restarted."""
    global _last_restart
    step = _last_stage1_step_sec()
    priv = _private_gb()
    _state.update(last_check=time.strftime("%H:%M:%S"), last_step_sec=step, private_gb=priv and round(priv, 1))
    reason = None
    if step is not None and step > SLOW_STEP_SEC:
        reason = f"video steps slowed to {step:.1f}s"
    if not reason or time.time() - _last_restart < 1800 or not _idle():
        return None
    # hold the GPU lock so nothing starts while it restarts (renders and the ahead-of-time pass just wait)
    with comfy_client._comfy_lock:
        if not comfy_client.restart_comfy():
            return None
    _last_restart = time.time()
    _state.update(restarts=_state["restarts"] + 1, last_reason=reason)
    return reason


def start() -> None:
    def loop():
        time.sleep(60)
        while True:
            try:
                check_once()
            except Exception:  # noqa: BLE001 - a check must never take the app down
                pass
            time.sleep(CHECK_EVERY_SEC)
    threading.Thread(target=loop, daemon=True, name="comfy-refresh").start()
