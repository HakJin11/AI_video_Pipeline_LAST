from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIR, MEDIA_DIR, ensure_dirs
from app import comfy_refresh, jobs, render_queue
from app.db import init_db, recover_stale_renders
from app.routes import (
    backgrounds,
    characters,
    composites,
    dialogues,
    follow,
    health,
    library_import,
    stories,
    translate,
    videos,
    voice_lines,
    voices,
)

ensure_dirs()
init_db()

app = FastAPI(title="AI Video Pipeline")


@app.on_event("startup")
def _start_render_worker() -> None:
    # Side effects that belong to the ONE server process only: fix statuses left behind by the
    # previous run, then start the render queue. Importing this module elsewhere must not do this.
    jobs.bind_loop()
    interrupted = recover_stale_renders()
    render_queue.start({"scene": stories._render_queued_scene, "cut": follow.render_queued_cut,
                        "cut_batch": follow.render_queued_cuts})
    follow.resume_interrupted(interrupted)
    comfy_refresh.start()  # restarts a ComfyUI that has gone slow, while nothing is being made
    from app import follow_render
    follow_render.ensure_motion_library()  # the tested 행동 lines (keeps their test results)

app.include_router(characters.router)
app.include_router(backgrounds.router)
app.include_router(composites.router)
app.include_router(dialogues.router)
app.include_router(voices.router)
app.include_router(voice_lines.router)
app.include_router(videos.router)
app.include_router(stories.router)
app.include_router(follow.router)
app.include_router(translate.router)
app.include_router(library_import.router)
app.include_router(health.router)

@app.middleware("http")
async def _revalidate_frontend(request, call_next):
    # the page itself must be re-checked on every load: browsers kept a cached index.html for hours
    # and ran yesterday's follow.js after an update (the ?v= cache-bust lives inside index.html).
    # Unchanged files still come back as a cheap 304.
    response = await call_next(request)
    path = request.url.path
    if not path.startswith(("/api/", "/media/")):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
