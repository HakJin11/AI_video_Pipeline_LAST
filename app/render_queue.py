"""Scene renders run one at a time, in the order they were asked for.

The GPU only ever runs one ComfyUI job at a time (comfy_client holds a lock), but every "렌더"
click used to start its own thread. So several scenes showed "렌더 중" at once while really just
waiting, their timers counted the wait, and a second click on a scene that was already rendering
rendered it twice in parallel -- the loser then crashed on the duplicate voice line and flagged a
scene whose video had come out fine as 실패.

Here a single worker thread drains a FIFO of scene ids. Waiting scenes are stored as status
'queued' so the UI can say 대기 instead of 렌더 중, and render_scene_clip only starts its timer when
the scene actually reaches the GPU.

A render request for the scene that is rendering RIGHT NOW is remembered, not dropped: the user
edited the lines mid-render and wants the new text. When the running pass ends, render_scene_clip
has already thrown its clip away if the scene changed underneath it, and the scene goes straight
back in line. (Dropping that request is how an edited scene ended up "완료" with the old dialogue.)
"""
import threading
from collections import deque
from typing import Callable

from app import db

# Story scenes and 유튜브 따라하기 cuts share this one worker (one GPU). Items are (kind, id);
# plain ints still mean a story scene.
_lock = threading.Lock()
_wake = threading.Event()
_pending: deque[tuple[str, int]] = deque()
_current: set[tuple[str, int]] = set()
# waiting cuts of one project taken together: their voices are made in one TTS pass, then
# render_cut_batch still makes the videos ONE cut per GPU job (2026-09-23: on the same 8-cut HQ run one
# at a time was no slower, 644s vs 651s, and the first cut showed up after 148s instead of 346s).
# 1 here would send each cut through render_cut, which reloads the TTS and video models every cut.
_BATCH = 4
_rerun: set[tuple[str, int]] = set()
_worker: threading.Thread | None = None

_SET_STATUS = {
    "scene": lambda i, **f: db.update_scene(i, **f),
    "cut": lambda i, **f: db.update_follow_cut(i, **f),
}
_GET = {"scene": db.get_scene, "cut": db.get_follow_cut}


def _key(item_id: int, kind: str) -> tuple[str, int]:
    return (kind, item_id)


def enqueue(item_id: int, kind: str = "scene") -> str:
    """Queue a scene/cut. One already waiting is not queued twice; the one rendering right now is
    re-queued once that pass ends, if its content changed meanwhile."""
    key = _key(item_id, kind)
    with _lock:
        if key in _current:
            _rerun.add(key)
            return "requeued"
        if key in _pending:
            return "queued"
        _pending.append(key)
    _SET_STATUS[kind](item_id, status="queued", error_message=None)
    _wake.set()
    return "queued"


def position(item_id: int, kind: str = "scene") -> int | None:
    """1-based place among everything still waiting, or None if not waiting."""
    with _lock:
        try:
            return list(_pending).index(_key(item_id, kind)) + 1
        except ValueError:
            return None


def _take_batch(first: tuple[str, int], render_fns: dict) -> list[tuple[str, int]]:
    """The first waiting cut plus up to _BATCH-1 more waiting cuts of the same project (called under
    _lock). Cut-by-cut clicks ("만들고 다음 컷") used to reload the voice and video models for every
    cut: a 20s run took 23 minutes (2026-09-22). Each cut still gets its own still, voice and prompt."""
    batch = [first]
    if first[0] != "cut" or "cut_batch" not in render_fns:
        return batch
    row = db.get_follow_cut(first[1])
    pid = row["project_id"] if row else None
    for key in list(_pending):
        if len(batch) >= _BATCH:
            break
        if key[0] == "cut":
            other = db.get_follow_cut(key[1])
            if other and other["project_id"] == pid:
                _pending.remove(key)
                batch.append(key)
    return batch


def _loop(render_fns: dict[str, Callable[[int], None]]) -> None:
    while True:
        _wake.wait()
        while True:
            with _lock:
                if not _pending:
                    _current.clear()
                    _wake.clear()
                    break
                batch = _take_batch(_pending.popleft(), render_fns)
                _current.update(batch)
            kind = batch[0][0]
            try:
                if len(batch) > 1:
                    render_fns["cut_batch"]([k[1] for k in batch])
                else:
                    render_fns[kind](batch[0][1])
            except Exception:  # noqa: BLE001 - the render function already stored status=error
                pass
            finally:
                with _lock:
                    _current.difference_update(batch)
                    reruns = [k for k in batch if k in _rerun]
                    _rerun.difference_update(batch)
            for kind_, item_id in reruns:
                row = _GET[kind_](item_id)
                # no clip means the renderer discarded it because the item was edited mid-way
                if row and not row["clip_path"]:
                    enqueue(item_id, kind_)


def start(render_fns: dict[str, Callable[[int], None]]) -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _worker = threading.Thread(target=_loop, args=(render_fns,), daemon=True, name="render-queue")
    _worker.start()
