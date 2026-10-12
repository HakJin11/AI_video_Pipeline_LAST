import subprocess
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import db, jobs, ollama_client, render_queue
from app.config import FFMPEG_PATH, MEDIA_DIR, STORY_MAX_SCENES
from app.ltx_prompt import build_prompt
from app.scene_render import render_scene_clip, scene_seconds
from app.story_assemble import assemble_story
from app.schemas import SceneAdd, SceneReorder, SceneUpdate, StoryCreate, StoryUpdate
from app.uploads import save_upload

router = APIRouter(prefix="/api/stories", tags=["stories"])


# ---------- detail resolution ----------

def _scene_detail(scene: dict) -> dict:
    bg = db.get_background(scene["background_id"])
    return {
        **scene,
        "background_name": bg["name"] if bg else None,
        "background_image_url": f"/media/{bg['image_path']}" if bg else None,
        "clip_url": f"/media/{scene['clip_path']}" if scene.get("clip_path") else None,
        # place in the render line ("대기 2번째"), None if not waiting
        "queue_pos": render_queue.position(scene["id"]) if scene.get("status") == "queued" else None,
    }


def _story_detail(story: dict) -> dict:
    c1 = db.get_character(story["character1_id"])
    c2 = db.get_character(story["character2_id"])
    v1 = db.get_voice(story["voice1_id"]) if story.get("voice1_id") else None
    v2 = db.get_voice(story["voice2_id"]) if story.get("voice2_id") else None
    raw_scenes = db.list_scenes(story["id"])
    scenes = [_scene_detail(s) for s in raw_scenes]
    # per-scene max characters for each speaker's line (its share of 총 길이 x that voice's pace),
    # shown live in the editor so a line that won't fit is visible while typing
    cps1, cps2 = _cast_voice_pace(story)
    line_limits = [
        [ollama_client.line_char_limit(scene_seconds(story, s["position"]), cps1),
         ollama_client.line_char_limit(scene_seconds(story, s["position"]), cps2)]
        for s in raw_scenes
    ]
    return {
        **story,
        "character1_name": c1["name"] if c1 else None,
        "character2_name": c2["name"] if c2 else None,
        "voice1_name": v1["name"] if v1 else None,
        "voice2_name": v2["name"] if v2 else None,
        "final_video_url": f"/media/{story['final_video_path']}" if story.get("final_video_path") else None,
        "music_url": f"/media/{story['music_path']}" if story.get("music_path") else None,
        "scenes": scenes,
        "line_limits": line_limits,
    }


def _cast_voice_pace(story: dict) -> tuple[float | None, float | None]:
    """Each cast voice's measured reading speed, so gemma writes lines that voice can actually say
    in the time the scene has. Falls back to None (the global constant) for an unmeasured voice."""
    out = []
    for key in ("voice1_id", "voice2_id"):
        v = db.get_voice(story[key]) if story.get(key) else None
        out.append(v["observed_chars_per_sec"] if v and v["observed_chars_per_sec"] else None)
    return out[0], out[1]


def _require_story(story_id: int) -> dict:
    story = db.get_story(story_id)
    if not story:
        raise HTTPException(404, "story not found")
    return story


# ---------- jobs ----------

@router.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


# ---------- CRUD ----------

@router.get("")
def list_stories():
    return db.list_stories()


@router.get("/{story_id}")
def get_story(story_id: int):
    return _story_detail(_require_story(story_id))


@router.post("")
def create_story(payload: StoryCreate):
    if not (db.get_character(payload.character1_id) and db.get_character(payload.character2_id)):
        raise HTTPException(404, "character not found")
    if not payload.background_ids:
        raise HTTPException(400, "at least one background is required")
    bgs = [db.get_background(b) for b in payload.background_ids]
    if any(b is None for b in bgs):
        raise HTTPException(404, "background not found")
    n = max(1, min(payload.scene_count, STORY_MAX_SCENES))

    story = db.create_story(
        payload.premise, payload.character1_id, payload.character2_id,
        payload.voice1_id, payload.voice2_id, n, payload.target_total_sec, payload.title,
        payload.fast_preview,
    )
    for i in range(n):
        bg_id = payload.background_ids[i % len(payload.background_ids)]
        prev_bg = payload.background_ids[(i - 1) % len(payload.background_ids)] if i else None
        # default staging: a background change reads as a jump in time/place -> dip through black;
        # staying in the same room -> a soft dissolve. Either can be overridden per scene later.
        transition = "blackout" if prev_bg is not None and bg_id != prev_bg else "fade"
        db.create_scene(story["id"], i, bg_id, transition=transition)
    return _story_detail(db.get_story(story["id"]))


@router.patch("/{story_id}")
def update_story(story_id: int, payload: StoryUpdate):
    _require_story(story_id)
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    db.update_story(story_id, **fields)
    return _story_detail(db.get_story(story_id))


@router.delete("/{story_id}")
def delete_story(story_id: int):
    _require_story(story_id)
    db.delete_story(story_id)
    return {"ok": True}


def _run_outline(story_id: int) -> dict:
    """Two-phase gemma authoring (beats + per-scene dialogue). Slow (1 + N calls, ~2 min for 6
    scenes) so it runs as a background job."""
    story = db.get_story(story_id)
    db.update_story(story_id, status="outlining", error_message=None)
    c1, c2 = db.get_character(story["character1_id"]), db.get_character(story["character2_id"])
    scenes = db.list_scenes(story_id)
    scene_settings = []
    for s in scenes:
        bg = db.get_background(s["background_id"])
        scene_settings.append(f"{bg['name']} - {bg['description']}" if bg else "미정")
    cps1, cps2 = _cast_voice_pace(story)
    try:
        outline = ollama_client.generate_story_outline(
            story["premise"], c1["name"], c1["description"], c2["name"], c2["description"],
            len(scenes), story["target_total_sec"], scene_settings, cps1, cps2,
        )
    except ollama_client.OllamaError as exc:
        db.update_story(story_id, status="error", error_message=f"gemma 대본 생성 실패: {exc}")
        raise

    for scene, beat in zip(scenes, outline):
        # new lines -> the old video prompt quotes dialogue that no longer exists; drop it
        db.update_scene(scene["id"], situation=beat["situation"], line1=beat["line1"], line2=beat["line2"],
                        ltx_prompt=None, lines_by_user=0)
        db.invalidate_scene_render(scene["id"])
    db.update_story(story_id, status="outlined")
    return db.get_story(story_id)


@router.post("/{story_id}/outline")
async def generate_outline(story_id: int):
    _require_story(story_id)
    job_id = jobs.start_job(_run_outline, story_id)
    return {"job_id": job_id}


# ---------- scenes ----------

def _prev_summary(story_id: int, before_position: int) -> str:
    lines = []
    for s in db.list_scenes(story_id):
        if s["position"] >= before_position:
            break
        if s["line1"] or s["line2"]:
            lines.append(f"{s['position'] + 1}. {s['situation']} / \"{s['line1']}\" - \"{s['line2']}\"")
    return "\n".join(lines)


@router.patch("/{story_id}/scenes/{scene_id}")
def update_scene(story_id: int, scene_id: int, payload: SceneUpdate):
    _require_story(story_id)
    scene = db.get_scene(scene_id)
    if not scene or scene["story_id"] != story_id:
        raise HTTPException(404, "scene not found")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if payload.background_id is not None and db.get_background(payload.background_id) is None:
        raise HTTPException(404, "background not found")
    # Only what actually differs from what's stored. The card sends every field on every save
    # ("렌더하고 다음 씬", "저장", "대사 다시" all save first) -- treating presence as a change used to
    # throw away a finished clip whenever the user merely clicked through a scene.
    changed = {k: v for k, v in fields.items() if scene.get(k) != v}
    if changed:
        db.update_scene(scene_id, **changed)
    if "line1" in changed or "line2" in changed:
        db.update_scene(scene_id, lines_by_user=1)   # typed by the user: never auto-shortened
    if any(k in changed for k in ("line1", "line2", "background_id", "situation")):
        db.invalidate_scene_render(scene_id)
        # a stored prompt quotes the old lines verbatim, so drop it unless this same edit set one
        if "ltx_prompt" not in changed:
            db.update_scene(scene_id, ltx_prompt=None)
    return _scene_detail(db.get_scene(scene_id))


@router.post("/{story_id}/scenes/{scene_id}/regenerate-lines")
def regenerate_scene_lines(story_id: int, scene_id: int):
    story = _require_story(story_id)
    scene = db.get_scene(scene_id)
    if not scene or scene["story_id"] != story_id:
        raise HTTPException(404, "scene not found")
    c1, c2 = db.get_character(story["character1_id"]), db.get_character(story["character2_id"])
    bg = db.get_background(scene["background_id"])
    situation = scene["situation"] or ""
    if bg:
        situation = f"장소: {bg['name']} ({bg['description']}). {situation}".strip()
    try:
        cps1, cps2 = _cast_voice_pace(story)
        line1, line2 = ollama_client.regenerate_scene_lines(
            story["premise"], c1["name"], c1["description"], c2["name"], c2["description"],
            _prev_summary(story_id, scene["position"]), situation,
            story["target_total_sec"], story["scene_count"], scene["position"] + 1, cps1, cps2,
        )
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"gemma 대사 생성 실패: {exc}") from exc
    db.update_scene(scene_id, line1=line1, line2=line2, ltx_prompt=None, lines_by_user=0)
    db.invalidate_scene_render(scene_id)
    return _scene_detail(db.get_scene(scene_id))


@router.post("/{story_id}/scenes/{scene_id}/prompt")
def build_scene_prompt(story_id: int, scene_id: int):
    """Author (or re-author) this scene's LTX prompt and store it, so it can be read and edited
    before the render instead of being generated and thrown away inside it. Character and
    background descriptions from the DB go in here."""
    story = _require_story(story_id)
    scene = db.get_scene(scene_id)
    if not scene or scene["story_id"] != story_id:
        raise HTTPException(404, "scene not found")
    if not (scene["line1"] and scene["line2"]):
        raise HTTPException(400, "이 씬에 대사가 없습니다")
    c1, c2 = db.get_character(story["character1_id"]), db.get_character(story["character2_id"])
    bg = db.get_background(scene["background_id"])
    # rough per-speaker seconds from character count; the render re-authors with measured audio
    # timings if the real TTS lands far from this estimate
    t1 = ollama_client.estimate_seconds(scene["line1"])
    t2 = ollama_client.estimate_seconds(scene["line2"])
    prompt = build_prompt(scene["situation"] or story["premise"], c1, c2, bg,
                          scene["line1"], scene["line2"], t1, t2)
    db.update_scene(scene_id, ltx_prompt=prompt)
    return _scene_detail(db.get_scene(scene_id))


@router.post("/{story_id}/scenes")
def add_scene(story_id: int, payload: SceneAdd):
    _require_story(story_id)
    if db.get_background(payload.background_id) is None:
        raise HTTPException(404, "background not found")
    story = db.get_story(story_id)
    scenes = db.list_scenes(story_id)
    if len(scenes) >= min(story["scene_count"], STORY_MAX_SCENES):
        raise HTTPException(
            400, f"이 스토리는 씬 {story['scene_count']}개로 설정되어 있습니다 (구성 단계에서 변경)"
        )
    pos = len(scenes) if payload.position is None else max(0, min(payload.position, len(scenes)))
    for s in scenes:
        if s["position"] >= pos:
            db.update_scene(s["id"], position=s["position"] + 1)
    db.create_scene(story_id, pos, payload.background_id)
    db.repack_scene_positions(story_id)
    db.update_story(story_id, scene_count=len(db.list_scenes(story_id)))
    return _story_detail(db.get_story(story_id))


@router.delete("/{story_id}/scenes/{scene_id}")
def delete_scene(story_id: int, scene_id: int):
    _require_story(story_id)
    scene = db.get_scene(scene_id)
    if not scene or scene["story_id"] != story_id:
        raise HTTPException(404, "scene not found")
    if len(db.list_scenes(story_id)) <= 1:
        raise HTTPException(400, "마지막 씬은 삭제할 수 없습니다")
    db.delete_scene(scene_id)
    db.repack_scene_positions(story_id)
    db.update_story(story_id, scene_count=len(db.list_scenes(story_id)))
    return _story_detail(db.get_story(story_id))


@router.post("/{story_id}/scenes/reorder")
def reorder_scenes(story_id: int, payload: SceneReorder):
    _require_story(story_id)
    db.reorder_scenes(story_id, payload.ordered_scene_ids)
    db.repack_scene_positions(story_id)
    return _story_detail(db.get_story(story_id))


# ---------- music ----------

@router.post("/{story_id}/music")
def upload_music(story_id: int, audio: UploadFile = File(...), gain: float = Form(0.25)):
    _require_story(story_id)
    rel = save_upload(audio, "stories")
    src = MEDIA_DIR / rel
    dst_rel = str(Path(rel).with_suffix(".m4a")).replace("\\", "/")
    dst = MEDIA_DIR / dst_rel
    try:
        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "160k", str(dst)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise HTTPException(400, f"오디오 파일을 처리할 수 없습니다: {exc}") from exc
    if src != dst:
        src.unlink(missing_ok=True)
    db.update_story(story_id, music_path=dst_rel, music_gain=gain)
    return _story_detail(db.get_story(story_id))


@router.delete("/{story_id}/music")
def delete_music(story_id: int):
    story = _require_story(story_id)
    if story.get("music_path"):
        (MEDIA_DIR / story["music_path"]).unlink(missing_ok=True)
    db.update_story(story_id, music_path=None)
    return _story_detail(db.get_story(story_id))


# ---------- render + assemble jobs ----------

def _render_queued_scene(scene_id: int) -> None:
    """Worker body for render_queue. Re-reads the scene when its turn comes, so edits made while
    it was waiting in line are what get rendered. Does NOT re-assemble the final video -- the
    완성 step's "최종 영상 만들기" does that once the user is ready."""
    scene = db.get_scene(scene_id)
    if not scene:
        return
    story = db.get_story(scene["story_id"])
    c1, c2 = db.get_character(story["character1_id"]), db.get_character(story["character2_id"])
    bg = db.get_background(scene["background_id"])
    render_scene_clip(scene, story, c1, c2, bg)


def _run_assemble(story_id: int) -> dict:
    story = db.get_story(story_id)
    final_rel = assemble_story(story, db.list_scenes(story_id))
    db.update_story(story_id, final_video_path=final_rel,
                    status="ready" if story["status"] != "draft" else story["status"])
    return db.get_story(story_id)


@router.post("/{story_id}/render")
async def render_story(story_id: int):
    story = _require_story(story_id)
    scenes = db.list_scenes(story_id)
    if any(not (s["line1"] and s["line2"]) for s in scenes):
        raise HTTPException(400, "모든 씬에 대사가 있어야 합니다 (아웃라인 생성 또는 직접 입력)")
    if not (story["voice1_id"] and story["voice2_id"]):
        raise HTTPException(400, "인물1/인물2 목소리를 먼저 지정하세요")
    queued = [s["id"] for s in scenes if not s["clip_path"]]
    for sid in queued:
        render_queue.enqueue(sid)
    return {"queued": queued}


@router.post("/{story_id}/scenes/{scene_id}/render")
async def render_scene(story_id: int, scene_id: int):
    _require_story(story_id)
    scene = db.get_scene(scene_id)
    if not scene or scene["story_id"] != story_id:
        raise HTTPException(404, "scene not found")
    if not (scene["line1"] and scene["line2"]):
        raise HTTPException(400, "이 씬에 대사가 없습니다")
    return {"state": render_queue.enqueue(scene_id)}


@router.post("/{story_id}/assemble")
async def assemble(story_id: int):
    _require_story(story_id)
    # only a complete story gets assembled -- a final video missing a scene, or built from a clip
    # that is being re-rendered right now, is never what the user wants
    scenes = db.list_scenes(story_id)
    missing = [s["position"] + 1 for s in scenes
               if not s["clip_path"] or s["status"] in ("queued", "rendering")]
    if missing:
        raise HTTPException(400, f"아직 안 만들어진 씬이 있습니다: 씬 {', '.join(map(str, missing))}")
    job_id = jobs.start_job(_run_assemble, story_id)
    return {"job_id": job_id}
