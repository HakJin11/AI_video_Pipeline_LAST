"""유튜브 따라하기 API: upload a reference clip -> cuts, cast (face + voice + costume), per-cut
prompt/render, final + side-by-side comparison."""
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import db, follow_render as fr, jobs, render_queue, prefetch
from app.config import MEDIA_DIR

router = APIRouter(prefix="/api/follow", tags=["follow"])

_media = lambda p: f"/media/{p}" if p else None  # noqa: E731


class ProjectPatch(BaseModel):
    title: str | None = None
    source_url: str | None = None
    setting: str | None = None
    background_id: int | None = None
    clear_background: bool = False
    fast_preview: bool | None = None
    use_still: bool | None = None
    published: bool | None = None
    description: str | None = None


class FromUrl(BaseModel):
    url: str
    title: str = ""
    start_sec: float = 0.0
    length_sec: float = 10.0
    cast_count: int = 3


class StartRun(BaseModel):
    length_sec: float | None = None


class Extend(BaseModel):
    length_sec: float = 30.0


LENGTH_CHOICES = (10, 20, 30)


class CastPatch(BaseModel):
    label: str | None = None
    character_id: int | None = None
    voice_id: int | None = None
    costume: str | None = None


class CutPatch(BaseModel):
    people: int | None = None
    cast_id2: int | None = None
    clear_cast2: bool = False
    framing: str | None = None
    camera: str | None = None
    cast_id: int | None = None
    clear_cast: bool = False
    look: str | None = None
    line: str | None = None
    action: str | None = None
    tone: str | None = None
    motion: str | None = None
    ltx_prompt: str | None = None


def _require(pid: int) -> dict:
    p = db.get_follow_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    return p


def _require_template(pid: int) -> dict:
    """Only admin presets are editable. A viewer's run keeps the admin's tuned prompts as they are:
    viewers pick a video and generate, they don't rewrite prompts (that is what made copies worse)."""
    p = _require(pid)
    if not p["is_template"]:
        raise HTTPException(403, "선택한 영상은 관리자가 맞춰 둔 프롬프트로만 만들 수 있습니다")
    return p


def _detail(p: dict) -> dict:
    # a viewer's run shows what the admin tuned as the recommendation for that cut
    rec_by_pos: dict[int, dict] = {}
    if p.get("template_id"):
        for t in db.list_follow_cuts(p["template_id"]):
            rec_by_pos[t["position"]] = {k: t[k] for k in ("tone", "framing", "camera", "motion", "look", "line",
                                                           "people")}
    cast = db.list_follow_cast(p["id"])
    cast_by_id = {c["id"]: c for c in cast}
    cuts = []
    for c in db.list_follow_cuts(p["id"]):
        member = cast_by_id.get(c["cast_id"])
        character = db.get_character(member["character_id"]) if member and member.get("character_id") else None
        voice = db.get_voice(member["voice_id"]) if member and member.get("voice_id") else None
        parts = fr.cut_prompt_parts(p, c, member, character)
        for part in parts:
            part["text_ko"] = db.get_translation(part["text"], "ko")
        auto = fr.join_prompt(parts)
        prompt = c["ltx_prompt"] or auto
        cuts.append({
            **c,
            "duration": round(c["end_sec"] - c["start_sec"], 2),
            "ref_clip_url": _media(c["ref_clip_path"]),
            "ref_thumb_url": _media(c["ref_thumb_path"]),
            "clip_url": _media(c["clip_path"]),
            "prompt_parts": parts,
            "auto_prompt": auto,
            "prompt": prompt,
            "prompt_ko": db.get_translation(prompt, "ko"),
            "line_en": db.get_translation(c["line"], "en") if c["line"] else None,
            "prompt_edited": bool(c["ltx_prompt"]),
            "line_limit": fr.line_char_limit(c, voice),
            "timing": json.loads(c["timing"]) if c.get("timing") else None,
            "recommended": rec_by_pos.get(c["position"]),
            "queue_pos": render_queue.position(c["id"], "cut") if c["status"] == "queued" else None,
        })
    # the preset's own person + voice for each role is the ⭐ in a viewer's cast pickers
    rec_cast = {t["label"]: {"character_id": t["character_id"], "voice_id": t["voice_id"]}
                for t in db.list_follow_cast(p["template_id"])} if p.get("template_id") else {}
    for m in cast:
        m["recommended"] = rec_cast.get(m["label"])
        ch = db.get_character(m["character_id"]) if m.get("character_id") else None
        m["character_image_url"] = _media(ch["image_path"]) if ch else None
        m["character_name"] = ch["name"] if ch else None
        m["still_url"] = _media(m["still_path"])
        if not m["still_url"] and ch:
            # made before for this person + outfit + place: show it right away instead of "첫 컷 만들 때 생성"
            m["still_url"] = _media(db.get_follow_still(fr.shared_still_key(m, p)))
    bg = db.get_background(p["background_id"]) if p.get("background_id") else None
    return {
        **p,
        # the place this run is set in: photo (if any) and the prompt in both languages
        "background": {"image_url": _media(bg["image_path"]) if bg else None, "name": bg["name"] if bg else None,
                       "prompt_en": p["setting"],
                       "prompt_ko": (bg.get("prompt_ko") if bg else None) or db.get_translation(p["setting"], "ko")},
        "ref_video_url": _media(p["ref_video_path"]),
        "final_video_url": _media(p["final_video_path"]),
        "compare_video_url": _media(p["compare_video_path"]),
        "default_setting": fr.DEFAULT_SETTING,
        # each 말투 carries the exact instruction that goes to Qwen3-TTS, so the page can show it
        "tone_options": [{"key": s["key"], "label": s["label"], "instruct": s.get("instruct", ""),
                          "sample_text": s.get("sample_text", "")} for s in fr.tone_styles()],
        "framing_options": [{"key": k, "label": v[1]} for k, v in fr.FRAMINGS.items()],
        "camera_options": [{"key": k, "label": v[1]} for k, v in fr.CAMERAS.items()],
        "motion_options": [{"key": k, "label": v[1]} for k, v in fr.MOTIONS.items()],
        "locked": not p["is_template"],
        "cast": cast,
        "cuts": cuts,
        # what the GPU is doing for this project right now (shown in the "만드는 중" bar)
        "render_stage": fr.RENDER_STAGE.get(p["id"]) if any(c["status"] in ("queued", "rendering")
                                                             for c in cuts) else None,
        # seconds left of that render (pictures, voices, videos and the final check), same numbers as "estimate"
        "eta": fr.render_eta(p, db.list_follow_cuts(p["id"])),
        # what is being made ahead of time, and which cuts are already made with these exact settings
        "prefetch": (pf := None if p["is_template"] else prefetch.status(p["id"])),
        # what "전부 만들기" would take now -- grows with a new place / person, shrinks as things are made ahead
        "estimate": fr.estimate_render(p, (pf or {}).get("ready_cuts", [])),
        "speed_warn": fr.speed_warnings(),
    }


@router.get("/progress")
def progress():
    """What the GPU is doing right now, for the "만드는 중" bar: ComfyUI's own step counter."""
    import httpx

    from app import comfy_client
    out = {"comfyui": False, "running": False, "step": 0, "total": 0, "queue": 0, "url": None}
    url = comfy_client.resolve_url()
    if not url:
        return out
    out["url"] = url
    try:
        r = httpx.get(f"{url}/queue", timeout=3)
        out["comfyui"] = r.status_code == 200
        q = r.json()
        out["running"] = bool(q.get("queue_running"))
        out["queue"] = len(q.get("queue_pending", []))
    except Exception:  # noqa: BLE001
        return out
    return out


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.get("")
def list_projects(kind: str = "template"):
    return db.list_follow_projects(is_template=(kind == "template"))


@router.get("/catalog")
def catalog():
    """영상 선택하기: published admin presets."""
    out = []
    for p in db.list_follow_projects(is_template=True, published_only=True):
        cuts = db.list_follow_cuts(p["id"])
        out.append({
            "id": p["id"], "title": p["title"], "description": p["description"], "length_sec": p["length_sec"],
            "cut_count": len(cuts), "cast_count": len(db.list_follow_cast(p["id"])),
            "thumb_url": _media(cuts[len(cuts) // 2]["ref_thumb_path"]) if cuts else None,
            "ref_video_url": _media(p["ref_video_path"]),
            # a choice is offered only if the preset's cuts actually reach that far
            "length_options": [n for n in LENGTH_CHOICES if p["length_sec"] >= n - 0.5] or [round(p["length_sec"])],
        })
    return out


@router.post("/catalog/{tid}/start")
async def start_from_catalog(tid: int, payload: StartRun | None = None):
    t = _require(tid)
    max_sec = payload.length_sec if payload and payload.length_sec else None
    if max_sec and max_sec > t["length_sec"] + 0.5:
        raise HTTPException(400, f"이 영상은 {round(t['length_sec'])}초까지만 준비되어 있습니다")
    if not (t["is_template"] and t["published"]):
        raise HTTPException(404, "선택할 수 없는 영상입니다")
    # only prompts the admin wrote by hand are frozen; the others are rebuilt from the run's own choices,
    # otherwise a viewer's 샷 크기/시선/말투/움직임/행동 changed nothing in the prompt (2026-09-30)
    frozen = {c["id"]: c["prompt"] for c in _detail(t)["cuts"] if c["prompt_edited"]}
    run = db.get_follow_project(db.copy_follow_project(tid, frozen, max_sec))
    if max_sec:
        db.update_follow_project(run["id"], ref_video_path=fr.ref_for_length(t, run["length_sec"]))
    # fill the Korean/English explanations in the background (instant when already cached), so a
    # new run never shows an untranslated prompt
    jobs.start_job(_fill_translations, run["id"])
    prefetch.request(run["id"])  # start making its stills, voices and clips while the viewer looks around
    return _detail(db.get_follow_project(run["id"]))


@router.get("/motions")  # before "/{pid}", which would take "motions" as a run id
def motions():
    """The 행동 lines with their test result (✅ = moved clearly, mouth in view, face kept toward the camera)."""
    return [{**m, "sample_url": _media(m["sample_path"])} for m in db.list_motions()]


@router.get("/{pid}")
def get_project(pid: int):
    return _detail(_require(pid))


@router.post("/{pid}/save-preset")
def save_preset(pid: int):
    """Snapshot the current cast, effective prompts, seeds and shot choices for future runs."""
    p = _require(pid)
    detail = _detail(p)
    if not detail["cuts"]:
        raise HTTPException(400, "저장할 컷이 없습니다")
    if any(c["status"] in ("queued", "rendering") for c in detail["cuts"]):
        raise HTTPException(409, "생성이 끝난 뒤 기준으로 저장해 주세요")
    new_id = db.copy_follow_project(pid, {c["id"]: c["prompt"] for c in detail["cuts"]})
    db.update_follow_project(new_id, is_template=1, published=1,
                             title=p["title"] + " · 저장한 기준",
                             description="저장한 캐스팅·구도·프롬프트·시드로 다시 만들기")
    return {"id": new_id, "title": db.get_follow_project(new_id)["title"]}


@router.post("")
def create_project(video: UploadFile = File(...), title: str = Form(""), source_url: str = Form(""),
                   start_sec: float = Form(0.0), length_sec: float = Form(10.0), cast_count: int = Form(3)):
    length_sec = max(2.0, min(length_sec, 30.0))
    project = db.create_follow_project(title or (video.filename or "따라하기"), source_url, "", start_sec,
                                       length_sec, fr.DEFAULT_SETTING)
    pdir = fr.project_dir(project["id"])
    src = pdir / f"source{Path(video.filename or '').suffix or '.mp4'}"
    with src.open("wb") as f:
        shutil.copyfileobj(video.file, f)
    return _build_project(project, src, start_sec, length_sec, cast_count)


@router.post("/from-url")
def create_from_url(payload: FromUrl):
    """Admin: fetch just [start, start+length] of a video page (yt-dlp) and cut it like an upload."""
    length = max(2.0, min(payload.length_sec, 30.0))
    project = db.create_follow_project(payload.title or payload.url, payload.url, "", payload.start_sec,
                                       length, fr.DEFAULT_SETTING)
    pdir = fr.project_dir(project["id"])
    try:
        src = fr.download_section(payload.url, payload.start_sec, length, pdir)
        if not payload.title:
            db.update_follow_project(project["id"], title=src.stem.split("__", 1)[-1] or payload.url)
    except Exception as exc:  # noqa: BLE001
        db.delete_follow_project(project["id"])
        shutil.rmtree(pdir, ignore_errors=True)
        raise HTTPException(400, f"영상 가져오기 실패: {exc}") from exc
    # the downloaded file already starts at start_sec
    return _build_project(db.get_follow_project(project["id"]), src, 0.0, length, payload.cast_count)


def _build_project(project: dict, src: Path, start_sec: float, length_sec: float, cast_count: int) -> dict:
    pdir = fr.project_dir(project["id"])
    try:
        ref = pdir / "ref.mp4"
        total = fr.trim_reference(src, ref, start_sec, length_sec)
        if total < 0.5:
            raise ValueError("시작 시간이 영상 길이보다 뒤에 있습니다")
        with db.get_conn() as conn:
            conn.execute("UPDATE follow_projects SET ref_video_path = ?, length_sec = ? WHERE id = ?",
                         (fr.rel(ref), round(total, 2), project["id"]))
        for i in range(max(1, min(cast_count, 4))):
            db.create_follow_cast(project["id"], "ABCD"[i])
        for i, (a, b) in enumerate(fr.plan_cuts(fr.detect_shot_changes(ref), total)):
            cut = db.create_follow_cut(project["id"], i, a, b)
            clip, thumb = pdir / f"ref_cut{i:02d}.mp4", pdir / f"ref_cut{i:02d}.jpg"
            fr.extract_ref_cut(ref, a, b, clip, thumb)
            people, shot = fr.detect_people_and_shot(thumb)
            db.update_follow_cut(cut["id"], ref_clip_path=fr.rel(clip), ref_thumb_path=fr.rel(thumb),
                                 camera=fr.detect_camera(clip), people=people, framing=shot)
    except Exception as exc:  # noqa: BLE001
        db.delete_follow_project(project["id"])
        shutil.rmtree(pdir, ignore_errors=True)
        raise HTTPException(400, f"영상 처리 실패: {exc}") from exc
    finally:
        src.unlink(missing_ok=True)
    return _detail(db.get_follow_project(project["id"]))


@router.patch("/{pid}")
def patch_project(pid: int, payload: ProjectPatch):
    p = _require(pid)
    fields = payload.model_dump(exclude_none=True, exclude={"clear_background"})
    if not p["is_template"]:
        # a viewer picks render quality (빠르게 / 고화질) for their own run, nothing else
        fields = {k: v for k, v in fields.items() if k in ("fast_preview", "use_still")}
        if not fields:
            raise HTTPException(403, "선택한 영상의 설정은 관리자만 바꿀 수 있습니다")
    for flag in ("fast_preview", "use_still"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    if "published" in fields:
        fields["published"] = 1 if fields["published"] else 0
    if payload.clear_background:
        fields["background_id"] = None
    look_changed = any(k in fields and fields[k] != p[k] for k in ("setting", "background_id"))
    with db.get_conn() as conn:
        for k, v in fields.items():
            if k in ("title", "source_url", "setting", "background_id", "fast_preview", "use_still",
                     "published", "description"):
                conn.execute(f"UPDATE follow_projects SET {k} = ? WHERE id = ?", (v, pid))
        if look_changed:  # every still/clip was made in the old place
            conn.execute("UPDATE follow_cast SET still_path = NULL WHERE project_id = ?", (pid,))
    prefetch.request(pid)
    return _detail(db.get_follow_project(pid))


@router.delete("/{pid}")
def delete_project(pid: int):
    _require(pid)
    prefetch.set_enabled(pid, False)
    db.delete_follow_project(pid)
    shutil.rmtree(fr.FOLLOW_DIR / str(pid), ignore_errors=True)
    return {"ok": True}


@router.post("/{pid}/cast")
def add_cast(pid: int):
    _require_template(pid)
    n = len(db.list_follow_cast(pid))
    db.create_follow_cast(pid, "ABCDEFGH"[min(n, 7)])
    return _detail(db.get_follow_project(pid))


@router.patch("/{pid}/cast/{cast_id}")
def patch_cast(pid: int, cast_id: int, payload: CastPatch):
    """Admin: everything. Viewer run: only WHO plays the role (face + voice) -- hair, outfit, place and
    prompts stay as the admin tuned them."""
    p = _require(pid)
    m = db.get_follow_cast(cast_id)
    if not m or m["project_id"] != pid:
        raise HTTPException(404, "cast not found")
    fields = payload.model_dump(exclude_none=True)
    if not p["is_template"]:
        fields = {k: v for k, v in fields.items() if k in ("character_id", "voice_id")}
    if fields.get("character_id") and not db.get_character(fields["character_id"]):
        raise HTTPException(404, "character not found")
    if fields.get("voice_id") and not db.get_voice(fields["voice_id"]):
        raise HTTPException(404, "voice not found")
    changes = {k: v for k, v in fields.items() if v != m.get(k)}
    if any(k in changes for k in ("character_id", "costume")):
        changes["still_path"] = None
    if "character_id" in changes and m.get("character_id"):
        # a hand-written prompt names the old person; point it at the new one
        old_who = fr.who_sentence(db.get_character(m["character_id"]))
        new_who = fr.who_sentence(db.get_character(changes["character_id"]))
        for c in db.list_follow_cuts(pid):
            if c["cast_id"] == cast_id and c["ltx_prompt"] and old_who in c["ltx_prompt"]:
                db.update_follow_cut(c["id"], ltx_prompt=c["ltx_prompt"].replace(old_who, new_who))
    if changes:
        db.update_follow_cast(cast_id, **changes)
        if any(k in changes for k in ("character_id", "voice_id", "costume")):
            # clips of this role show the old person / voice
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE follow_cuts SET clip_path = NULL, clip_sec = NULL, spoken_line = NULL, render_sec = NULL, "
                    "status = CASE WHEN status IN ('queued','rendering') THEN status ELSE 'pending' END "
                    "WHERE project_id = ? AND cast_id = ?", (pid, cast_id))
        prefetch.request(pid)
    return _detail(db.get_follow_project(pid))


def _make_still(pid: int, cast_id: int) -> dict:
    fr.ensure_still(db.get_follow_project(pid), db.get_follow_cast(cast_id))
    return {"ok": True}


@router.post("/{pid}/cast/{cast_id}/still")
async def remake_still(pid: int, cast_id: int):
    """Queue-free still preview: lets people check face + costume before spending clip renders."""
    _require_template(pid)
    m = db.get_follow_cast(cast_id)
    if not m or m["project_id"] != pid:
        raise HTTPException(404, "cast not found")
    db.update_follow_cast(cast_id, still_path=None)
    return {"job_id": jobs.start_job(_make_still, pid, cast_id)}


@router.delete("/{pid}/cast/{cast_id}")
def delete_cast(pid: int, cast_id: int):
    _require_template(pid)
    db.delete_follow_cast(cast_id)
    return _detail(db.get_follow_project(pid))


@router.patch("/{pid}/cuts/{cut_id}")
def patch_cut(pid: int, cut_id: int, payload: CutPatch):
    """Admin: everything. Viewer run: the acting choices (line, gaze, tone, motion, action) but not the
    prompt text itself."""
    p = _require(pid)
    c = db.get_follow_cut(cut_id)
    if not c or c["project_id"] != pid:
        raise HTTPException(404, "cut not found")
    fields = payload.model_dump(exclude_none=True, exclude={"clear_cast", "clear_cast2"})
    if not p["is_template"]:
        fields = {k: v for k, v in fields.items()
                  if k in ("look", "line", "action", "tone", "motion", "framing", "camera", "ltx_prompt",
                           "people", "cast_id2")}
    if fields.get("framing") is not None and fields["framing"] not in fr.FRAMINGS:
        raise HTTPException(400, "모르는 구도입니다")
    if fields.get("camera") is not None and fields["camera"] not in fr.CAMERAS:
        raise HTTPException(400, "모르는 카메라 움직임입니다")
    if fields.get("tone") is not None and fields["tone"] not in {s["key"] for s in fr.tone_styles()}:
        raise HTTPException(400, "모르는 말투입니다")
    if fields.get("motion") is not None and fields["motion"] not in fr.MOTIONS:
        raise HTTPException(400, "모르는 움직임입니다")
    if payload.clear_cast2:
        fields["cast_id2"] = None
    if payload.clear_cast:
        fields["cast_id"] = None
    if "look" in fields and fields["look"] not in ("left", "right"):
        raise HTTPException(400, "look must be left or right")
    if "ltx_prompt" in fields and not fields["ltx_prompt"].strip():
        fields["ltx_prompt"] = None  # empty box = go back to the automatic prompt
    changes = {k: v for k, v in fields.items() if v != c.get(k)}
    if "ltx_prompt" in fields and fields["ltx_prompt"] is None and c.get("ltx_prompt"):
        changes["ltx_prompt"] = None
    if changes:
        with db.get_conn() as conn:
            for k, v in changes.items():
                conn.execute(f"UPDATE follow_cuts SET {k} = ? WHERE id = ?", (v, cut_id))
            conn.execute(
                "UPDATE follow_cuts SET clip_path = NULL, clip_sec = NULL, spoken_line = NULL, "
                "status = CASE WHEN status IN ('queued','rendering') THEN status ELSE 'pending' END, "
                "error_message = NULL WHERE id = ?", (cut_id,))
        prefetch.request(pid)
    return _detail(db.get_follow_project(pid))


@router.post("/{pid}/cuts/{cut_id}/render")
def render_cut(pid: int, cut_id: int):
    _require(pid)
    c = db.get_follow_cut(cut_id)
    if not c or c["project_id"] != pid:
        raise HTTPException(404, "cut not found")
    if not c["cast_id"]:
        raise HTTPException(400, "이 컷에 나올 인물을 먼저 고르세요")
    return {"state": render_queue.enqueue(cut_id, "cut")}


# One "전부 만들기" per project at a time. Clicking it again (or from a second tab) used to start
# another job, and every extra job piled more prompts into ComfyUI until the waits timed out.
_ACTIVE_RENDERS: dict[int, str] = {}


def _render_all_job(pid: int, todo: list[int]) -> dict:
    """Voices first (one TTS load for the whole project), then the clips a few per GPU job.
    The cuts were already marked 대기 when the button was pressed."""
    import time
    started = time.monotonic()
    fr.RENDER_STAGE[pid] = {"started": time.time(), "text": "준비 중"}
    try:
        try:  # all pictures first, then all voices, then all videos: each model is loaded once
            fr.prewarm_stills(pid, only=todo)
        except Exception:  # noqa: BLE001 - only a speed-up; each cut makes its own still if this failed
            pass
        try:
            result = fr.prewarm_voices(pid)
        except Exception:  # noqa: BLE001 - only a speed-up; each cut makes its own voice if this failed
            result = {"voices": 0}
        todo = [cid for cid in todo if (c := db.get_follow_cut(cid)) and not c["clip_path"]]
        out = {**result, **fr.render_cut_batch(todo), "queued": len(todo)}
    except Exception as exc:
        # anything left at 대기/만드는 중 would sit there forever (seen 2026-09-22 when gemma dropped out)
        for cid in todo:
            c = db.get_follow_cut(cid)
            if c and c["status"] in ("queued", "rendering"):
                db.update_follow_cut(cid, status="error", error_message=fr.friendly_error(exc))
        raise
    db.update_follow_project(pid, last_render_sec=round(time.monotonic() - started, 1))
    fr.RENDER_STAGE.pop(pid, None)
    jobs.start_job(_fill_translations, pid)  # the lesson notes, now that the GPU is free
    return out


def start_render_all(pid: int) -> dict:
    cuts = db.list_follow_cuts(pid)
    running = _ACTIVE_RENDERS.get(pid)
    if running:
        job = jobs.get_job(running)
        if job and job["status"] in ("queued", "running"):
            return {"job_id": running, "already_running": True,
                    "queued": [c["id"] for c in cuts if not c["clip_path"]]}
    # show 대기 on every waiting cut right away -- the voice pass takes a minute before any video
    # starts, and a page that still said "아직 안 만든 컷" looked like the button did nothing
    todo = [c["id"] for c in cuts if not c["clip_path"] and c["status"] not in ("queued", "rendering")]
    job_id = jobs.start_job(_render_all_job, pid, todo)  # first: if this fails, nothing is left at 대기
    for cid in todo:
        db.update_follow_cut(cid, status="queued", error_message=None)
    _ACTIVE_RENDERS[pid] = job_id
    return {"job_id": job_id, "queued": [c["id"] for c in cuts if not c["clip_path"]]}


def resume_interrupted(pids: list[int]) -> None:
    """After a restart (watchdog, crash, update) carry on with the renders that were cut off, instead of
    leaving the viewer's run half made -- on 2026-09-22 a user's run stopped after 1 of 8 cuts when the
    server was restarted. Waits for ComfyUI first; the watchdog may still be starting it."""
    import threading
    import time

    from app import comfy_client

    def go():
        for _ in range(36):
            if comfy_client.resolve_url(force=True):
                break
            time.sleep(5)
        for pid in pids:
            if db.get_follow_project(pid) and all(c["cast_id"] for c in db.list_follow_cuts(pid)):
                start_render_all(pid)
    if pids:
        threading.Thread(target=go, daemon=True, name="resume-renders").start()


class PrefetchSwitch(BaseModel):
    on: bool | None = None  # None = the page was opened: carry on where it stopped (e.g. after a restart)


@router.post("/{pid}/prefetch")
def prefetch_switch(pid: int, payload: PrefetchSwitch):
    p = _require(pid)
    if p["is_template"]:
        return {"prefetch": None}
    if payload.on is None:
        prefetch.resume(pid)
    else:
        prefetch.set_enabled(pid, payload.on)
    return {"prefetch": prefetch.status(pid)}


@router.post("/{pid}/prefetch/hold")
def prefetch_hold(pid: int):
    """The viewer started typing in a box that ✨ cleans up: keep the GPU free for that for a little while."""
    _require(pid)
    prefetch.touch()
    prefetch.hold()
    return {"ok": True}


@router.post("/{pid}/render-all")
async def render_all(pid: int):
    _require(pid)
    cuts = db.list_follow_cuts(pid)
    missing = [c["position"] + 1 for c in cuts if not c["cast_id"]]
    if missing:
        raise HTTPException(400, f"인물이 안 정해진 컷: {', '.join(map(str, missing))}")
    return start_render_all(pid)


def _run_assemble(pid: int) -> dict:
    p = db.get_follow_project(pid)
    final, compare = fr.assemble(p, db.list_follow_cuts(pid))
    db.update_follow_project(pid, final_video_path=final, compare_video_path=compare)
    return {"ok": True}


@router.post("/{pid}/assemble")
async def assemble(pid: int):
    _require(pid)
    missing = [c["position"] + 1 for c in db.list_follow_cuts(pid)
               if not c["clip_path"] or c["status"] in ("queued", "rendering")]
    if missing:
        raise HTTPException(400, f"아직 안 만든 컷: {', '.join(map(str, missing))}")
    return {"job_id": jobs.start_job(_run_assemble, pid)}


def _rescan_job(pid: int) -> dict:
    """Re-read every original cut: how many people, how tight the shot, how the camera moves."""
    changed = 0
    for c in db.list_follow_cuts(pid):
        if not c["ref_thumb_path"]:
            continue
        people, shot = fr.detect_people_and_shot(MEDIA_DIR / c["ref_thumb_path"])
        camera = fr.detect_camera(MEDIA_DIR / c["ref_clip_path"]) if c["ref_clip_path"] else "locked"
        if (people, shot, camera) != (c["people"], c["framing"], c["camera"]):
            db.update_follow_cut(c["id"], people=people, framing=shot, camera=camera)
            changed += 1
    return {"updated": changed}


def _mimic_job(pid: int) -> dict:
    """Watch every original cut and copy its acting (gaze side, emotion -> tone, one movement)."""
    changed = []
    for c in db.list_follow_cuts(pid):
        if not c["ref_clip_path"]:
            continue
        info = fr.describe_original_cut(MEDIA_DIR / c["ref_clip_path"], c["line"] or "")
        if not info:
            continue
        fields = {"action": info["action"]} if info["action"] else {}
        if info["look"]:
            fields["look"] = info["look"]
        # the vision model's emotion word is unreliable (it tends to answer one word for every
        # cut); the admin's tone choice stays. Its concrete action and gaze side are what we copy.
        if fields:
            db.update_follow_cut(c["id"], **fields)
            changed.append({"cut": c["position"] + 1, **info})
    return {"updated": len(changed), "cuts": changed}


@router.post("/{pid}/mimic")
async def mimic(pid: int):
    """관리자: 원본 연기 따라하기 -- read each original cut's acting into the cut settings."""
    _require_template(pid)
    return {"job_id": jobs.start_job(_mimic_job, pid)}


@router.post("/{pid}/rescan")
async def rescan(pid: int):
    _require_template(pid)
    return {"job_id": jobs.start_job(_rescan_job, pid)}


def _renders_active() -> bool:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM follow_cuts WHERE status IN ('queued','rendering') LIMIT 1").fetchone() is not None


def _fill_translations(pid: int) -> dict:
    """The Korean notes for the lesson panel. They wait for renders: gemma serves one request at a time,
    and ~50 background translations queued in front of a run's line preparation held a 10s run at
    "대기" for 11 minutes (2026-09-22). A finished render-all starts this again."""
    from app import ollama_client
    todo: list[tuple[str, str]] = []
    for c in _detail(db.get_follow_project(pid))["cuts"]:
        if c["line"] and not c["line_en"]:
            todo.append((c["line"], "en"))
        if not c["prompt_ko"]:
            todo.append((c["prompt"], "ko"))
        todo += [(part["text"], "ko") for part in c["prompt_parts"] if not part["text_ko"]]
    import time

    from app import comfy_client
    done = 0
    for src, lang in dict.fromkeys(todo):
        if db.get_translation(src, lang):
            continue
        # only while nobody needs gemma or the GPU: these notes ran gemma on the CPU right while the viewer pressed
        # "AI가 행동 정하기", which then waited 22-39 s per cut behind them (20 s run, 2026-10-06)
        waited = time.time()
        while (_renders_active() or comfy_client._comfy_lock.locked() or comfy_client.foreground_busy()
               or time.time() - prefetch._last_activity < 90):
            if time.time() - waited > 3600:
                return {"translated": done, "deferred": True}
            time.sleep(5)
        if db.get_translation(src, lang):
            continue
        db.put_translation(src, lang, ollama_client.translate(src, lang))
        done += 1
    return {"translated": done}


@router.post("/{pid}/translate")
async def translate(pid: int):
    """Korean line -> English and English prompt -> Korean, cached by text so presets and every
    viewer run copied from them share the same translations."""
    _require(pid)
    return {"job_id": jobs.start_job(_fill_translations, pid)}


def _extend(pid: int, length: float) -> dict:
    """Admin: make a preset longer (e.g. 10s -> 30s) without touching the cuts already tuned. Fetches
    [start, start+length] again, keeps every existing cut, and cuts only the new tail."""
    p = db.get_follow_project(pid)
    pdir = fr.project_dir(pid)
    old_end = p["length_sec"]
    for f in pdir.glob("source__*"):
        f.unlink()
    src = fr.download_section(p["source_url"], p["source_start_sec"], length, pdir)
    tmp = pdir / "ref_new.mp4"
    total = fr.trim_reference(src, tmp, 0.0, length)
    src.unlink(missing_ok=True)
    changes = [t for t in fr.detect_shot_changes(tmp) if t > old_end + 0.05]
    tail = [(a + old_end, b + old_end) for a, b in fr.plan_cuts([t - old_end for t in changes], total - old_end)]
    ref = pdir / "ref.mp4"
    ref.unlink(missing_ok=True)
    tmp.rename(ref)
    cuts = db.list_follow_cuts(pid)
    pos = len(cuts)
    for a, b in tail:
        cut = db.create_follow_cut(pid, pos, round(a, 3), round(b, 3))
        clip, thumb = pdir / f"ref_cut{pos:02d}.mp4", pdir / f"ref_cut{pos:02d}.jpg"
        fr.extract_ref_cut(ref, a, b, clip, thumb)
        db.update_follow_cut(cut["id"], ref_clip_path=fr.rel(clip), ref_thumb_path=fr.rel(thumb))
        pos += 1
    with db.get_conn() as conn:
        conn.execute("UPDATE follow_projects SET length_sec = ?, final_video_path = NULL, compare_video_path = NULL "
                     "WHERE id = ?", (round(total, 2), pid))
    return {"added": len(tail), "length_sec": round(total, 2)}


@router.post("/{pid}/extend")
async def extend(pid: int, payload: Extend):
    p = _require_template(pid)
    if not p["source_url"]:
        raise HTTPException(400, "유튜브 주소로 가져온 영상만 늘릴 수 있습니다")
    if payload.length_sec <= p["length_sec"] + 0.5:
        raise HTTPException(400, "지금보다 긴 길이를 고르세요")
    return {"job_id": jobs.start_job(_extend, pid, min(payload.length_sec, 30.0))}


def render_queued_cuts(cut_ids: list[int]) -> None:
    import time
    cuts = [c for c in (db.get_follow_cut(i) for i in cut_ids) if c]
    if not cuts:
        return
    pid = cuts[0]["project_id"]
    fr.RENDER_STAGE.setdefault(pid, {"started": time.time(), "text": "준비 중"})
    try:
        try:
            fr.prewarm_stills(pid, only=[c["id"] for c in cuts])
        except Exception:  # noqa: BLE001 - only a speed-up
            pass
        try:
            fr.prewarm_voices(pid, only=[c["id"] for c in cuts])
        except Exception:  # noqa: BLE001 - only a speed-up
            pass
        fr.render_cut_batch([c["id"] for c in cuts])
    finally:
        if not any(x["status"] in ("queued", "rendering") for x in db.list_follow_cuts(pid)):
            fr.RENDER_STAGE.pop(pid, None)


def render_queued_cut(cut_id: int) -> None:
    import time
    c = db.get_follow_cut(cut_id)
    if not c:
        return
    pid = c["project_id"]
    fr.RENDER_STAGE.setdefault(pid, {"started": time.time(), "text": "준비 중"})
    try:
        fr.render_cut(cut_id)
    finally:
        if not any(x["status"] in ("queued", "rendering") for x in db.list_follow_cuts(pid)):
            fr.RENDER_STAGE.pop(pid, None)


# ---------------------------------------------------------------- 배경: 예시 · 직접 쓰기 · ChatGPT로 만들기
# (2026-09-30, user) viewers set the place themselves: pick an example photo, write the place in Korean or
# English, or have ChatGPT's image model draw it; the people are then put into that place by the still step.

BG_TIPS = [
    {"ko": "장소 이름부터: '한강 공원 벤치', '편의점 계산대 앞'처럼 어디인지 먼저 적어요.",
     "en": "Start with the place: 'a bench in a Han River park', 'in front of a convenience-store counter'."},
    {"ko": "눈에 보이는 것 3~5개: 가구·소품·벽 색깔을 쉼표로 이어 적으면 그대로 그려져요.",
     "en": "3-5 things you can see: furniture, props, wall colours, separated by commas."},
    {"ko": "빛과 시간대: '저녁 노을빛', '형광등 아래', '비 오는 오후'처럼 조명을 적으면 분위기가 살아요.",
     "en": "Light and time of day: 'warm sunset light', 'under fluorescent lights', 'a rainy afternoon'."},
    {"ko": "사람은 쓰지 마세요: 인물은 다음 단계에서 넣어요. 글자·간판도 영상에 깨져 나와서 빼는 게 좋아요.",
     "en": "Leave people out (they are added next). Avoid readable signs or text; they come out garbled."},
]
BG_WRITING_EXAMPLES = [
    {"ko": "비 오는 저녁 편의점 안. 계산대와 컵라면 진열대, 창밖에 젖은 거리 불빛, 형광등 조명",
     "en": "inside a convenience store on a rainy evening: the counter, shelves of cup noodles, wet street lights "
           "through the window, fluorescent lighting"},
    {"ko": "한강 공원 잔디밭. 멀리 다리와 강, 돗자리와 편의점 봉지, 따뜻한 노을빛",
     "en": "a lawn in a Han River park: a bridge and the river in the distance, a picnic mat and convenience-store "
           "bags, warm sunset light"},
    {"ko": "대학교 강의실. 계단식 책상, 앞쪽 큰 칠판과 스크린, 창으로 들어오는 오후 햇빛",
     "en": "a university lecture hall: tiered desks, a large blackboard and projector screen at the front, "
           "afternoon sunlight through the windows"},
]


class OpenAISettings(BaseModel):
    api_key: str | None = None
    model: str | None = None


class BgText(BaseModel):
    text: str
    engine: str = "local"  # "local" = Z-Image-Turbo on this PC (free); "openai" = paid API (needs a key)
    en: str | None = None  # already cleaned up on the page (✨): use as it is
    name: str | None = None
    refine: bool = True  # tidy a rough description with gemma before drawing


class BgUse(BaseModel):
    background_id: int | None = None
    original: bool = False  # back to the preset's own place


def _has_korean(text: str) -> bool:
    import re
    return bool(re.search(r"[가-힣]", text or ""))


def _both_languages(text: str) -> tuple[str, str]:
    """(korean, english) for a place description written in either language."""
    from app import ollama_client
    text = text.strip()
    if _has_korean(text):
        return text, ollama_client.translate_prompt_text(text, "en")
    return ollama_client.translate_prompt_text(text, "ko"), text


def _apply_background(pid: int, setting: str, background_id: int | None) -> None:
    """New place for a run: stills are drawn again and made clips (old place) go back to 아직."""
    with db.get_conn() as conn:
        conn.execute("UPDATE follow_projects SET setting = ?, background_id = ? WHERE id = ?",
                     (setting, background_id, pid))
        conn.execute("UPDATE follow_cast SET still_path = NULL WHERE project_id = ?", (pid,))
        conn.execute("UPDATE follow_cuts SET clip_path = NULL, clip_sec = NULL, status = 'pending', "
                     "error_message = NULL WHERE project_id = ? AND status NOT IN ('queued', 'rendering')", (pid,))
    prefetch.request(pid)  # the new place's stills right away, while the viewer looks at the picture


def _require_idle(pid: int) -> dict:
    p = _require(pid)
    if any(c["status"] in ("queued", "rendering") for c in db.list_follow_cuts(pid)):
        raise HTTPException(409, "영상을 만드는 중에는 배경을 바꿀 수 없어요. 끝난 뒤 바꿔 주세요.")
    return p


@router.get("/settings/openai")
def openai_settings():
    from app import openai_images
    return openai_images.status()


@router.put("/settings/openai")
def save_openai_settings(payload: OpenAISettings):
    from app import openai_images
    return openai_images.save_settings(payload.api_key, payload.model)


@router.get("/backgrounds/examples")
def background_examples():
    """Saved places with their photo and the prompt in Korean and English, plus how to write one."""
    from app import openai_images
    rows = [b for b in db.list_backgrounds() if b.get("is_example") or b.get("source") in ("gpt", "local")]
    rows.sort(key=lambda b: (-(b.get("recommended") or 0), -b["id"]))  # ⭐ first
    # the same place made twice (e.g. "비 오는 밤 편의점" x2 from tests) is listed once, the newest picture
    seen: set = set()
    rows = [b for b in rows if not ((k := (b["name"] or "").strip()) in seen or seen.add(k))]
    return {
        "examples": [{"id": b["id"], "name": b["name"], "image_url": _media(b["image_path"]),
                      "prompt_ko": b.get("prompt_ko") or "", "prompt_en": b["description"],
                      "source": b.get("source") or "upload", "recommended": bool(b.get("recommended"))}
                     for b in rows],
        "tips": BG_TIPS, "writing_examples": BG_WRITING_EXAMPLES,
        "openai": openai_images.status(),
    }


@router.post("/{pid}/background/use")
def use_background(pid: int, payload: BgUse):
    p = _require_idle(pid)
    if payload.original:
        t = db.get_follow_project(p["template_id"]) if p.get("template_id") else None
        _apply_background(pid, (t or p)["setting"], (t or {}).get("background_id"))
    else:
        b = db.get_background(payload.background_id) if payload.background_id else None
        if not b:
            raise HTTPException(404, "배경을 찾을 수 없습니다")
        _apply_background(pid, b["description"], b["id"])
        jobs.start_job(_stills_for_place, pid)  # saved place: its pictures may be made already (then instant)
    return _detail(db.get_follow_project(pid))


def _write_background(pid: int, text: str) -> dict:
    """A written place now always gets its picture drawn (free, Z-Image): with words only, every cut's still
    re-imagined the place differently, and with no photo the face went into all three image slots of the still
    step -- the store run showed the same person twice in both wide cuts (2026-09-30)."""
    return _generate_background(pid, text, "local")


def _generate_background(pid: int, text: str, engine: str = "local", en: str | None = None, name: str | None = None,
                         refine: bool = True) -> dict:
    from app import comfy_client, local_images, ollama_client, openai_images
    with comfy_client.foreground():  # the viewer is waiting: things made ahead of time step aside
        refined = None
        if en:
            ko = text.strip()
        elif refine:
            try:  # a rough "비오는 편의점 밤" becomes a full empty-place description (✨, 2026-10-02)
                refined = ollama_client.refine_background(text)
                ko, en, name = refined["ko"], refined["en"], refined["name"]
            except Exception:  # noqa: BLE001 - plain translation then
                ko, en = _both_languages(text)
        else:
            ko, en = _both_languages(text)
        return _draw_background(pid, ko, en, engine, name, refined)


def _draw_background(pid: int, ko: str, en: str, engine: str, name: str | None, refined: dict | None) -> dict:
    from app import local_images, openai_images
    if engine == "openai":
        path, source = openai_images.generate_background(en), "gpt"
    else:
        path, source = local_images.generate_background(en), "local"
    name = name or (ko if len(ko) <= 16 else ko[:15] + "…")
    b = db.create_background(name, en, fr.rel(path), ko, source)
    _apply_background(pid, en, b["id"])
    # the people in the new place are drawn right away (one picture each, ~1 min), while the viewer looks at the
    # place and edits the cuts -- not when "만들기" is pressed (2026-10-06: 8 cuts in a new place in 10-14 min)
    jobs.start_job(_stills_for_place, pid)
    return {"background_id": b["id"], "image_url": _media(b["image_path"]), "prompt_ko": ko, "prompt_en": en,
            "refined": bool(refined)}


def _stills_for_place(pid: int) -> dict:
    from app import comfy_client
    # the "미리 만들기" box shows these pictures as its first step instead of "⏸ 지금 하시는 작업이 먼저예요"
    prefetch.place_stills(pid, True)
    try:
        with comfy_client.foreground():
            return fr.prewarm_stills(pid, quiet=True, progress=lambda i, n: prefetch.place_stills(pid, True, (i, n)))
    finally:
        prefetch.place_stills(pid, False)


@router.post("/{pid}/background/write")
async def write_background(pid: int, payload: BgText):
    """Kept for old pages: same as generate (the place is always drawn as a picture now)."""
    _require_idle(pid)
    if not payload.text.strip():
        raise HTTPException(400, "배경을 적어 주세요")
    return {"job_id": jobs.start_job(_write_background, pid, payload.text)}


@router.post("/{pid}/background/generate")
async def generate_background(pid: int, payload: BgText):
    """The place is drawn from the text and becomes this run's background photo. Default: Z-Image-Turbo on
    this PC (free). The paid OpenAI image API stays available with engine="openai" (not on the page)."""
    from app import openai_images
    _require_idle(pid)
    if not payload.text.strip():
        raise HTTPException(400, "만들 배경을 적어 주세요")
    if payload.engine == "openai" and not openai_images.api_key():
        raise HTTPException(400, "OpenAI API 키가 없습니다.")
    return {"job_id": jobs.start_job(_generate_background, pid, payload.text, payload.engine, payload.en,
                                     payload.name, payload.refine)}


def _refine_prompt(p: dict, cut: dict, cast: dict | None, character: dict | None, text: str) -> dict:
    """gemma gives the changed parts; the prompt is rebuilt here (see ollama_client.refine_shot)."""
    import re
    from app import ollama_client
    current = cut.get("ltx_prompt") or fr.join_prompt(fr.cut_prompt_parts(p, cut, cast, character))
    cam_now = next((l[len("camera:"):].strip() for l in current.splitlines() if l.startswith("camera:")), "")
    r = ollama_client.refine_shot(text, cut.get("action") or "", cam_now,
                                  (p.get("setting") or fr.DEFAULT_SETTING).split(":")[0], fr._pron(character))
    if cut.get("ltx_prompt"):  # a hand-written prompt: change its action line in place
        lines = current.splitlines()
        if r["action"]:
            act = fr.closeup_action(r["action"], fr._pron(character)) if (cut.get("framing") or "") == "closeup"                 else r["action"]
            lines = [f"action: {act}, hands stay away from the face" if l.startswith("action:") else l
                     for l in lines]
        out = "\n".join(lines)
    else:  # the automatic prompt, rebuilt with the new action (gaze wording follows it)
        out = fr.join_prompt(fr.cut_prompt_parts(p, {**cut, "action": r["action"] or cut.get("action")}, cast, character))
    shot = fr.FRAMINGS.get(cut.get("framing") or "medium", fr.FRAMINGS["medium"])[2]
    lines = out.splitlines()
    if r["camera"]:
        lines = [f"camera: {r['camera']}, no cut, {shot}" if l.startswith("camera:") else l for l in lines]
    if r["scene_extra"]:
        lines = [f"{l}, {r['scene_extra']}" if l.startswith("scene:") else l for l in lines]
    out = re.sub(r"\bKorean\s+", "", "\n".join(lines))
    warn = None
    if r["adds_person"] and not (cut.get("people") or 1) >= 2:
        warn = ("두 사람이 같이 나오게 하려면 컷 설정의 '화면 속 인원: 2명'을 고르세요 — 프롬프트만 바꾸면 첫 장면에 없던 "
                "친구가 걸어 들어오는 영상이 되기 쉬워요")
    elif r["looks_into_camera"]:
        warn = "카메라를 보고 말하게 바꿨어요 — 원하신 게 아니면 '친구를 보며'라고 다시 적어 주세요"
    return {"prompt": out, "ko": r["ko"], "warn": warn, "action": r["action"], "sec": r["sec"], "gpu": r["gpu"]}


class RefineIn(BaseModel):
    kind: str  # "background" | "action" | "prompt"
    text: str
    cut_id: int | None = None


def _pick_actions(cuts: list[dict]) -> dict:
    """gemma picks one TESTED motion (db.motion_library, verified) per cut; the cut gets its full, detailed line.
    Falls back to gemma's own short lines only if no motion has been verified yet."""
    from app import ollama_client
    lib = [m for m in db.list_motions() if m["verified"]]
    tones = {t["key"]: t.get("label", "") for t in fr.tone_styles()}
    info = []
    for c in cuts:
        cast = db.get_follow_cast(c["cast_id"])
        ch = db.get_character(cast["character_id"]) if cast and cast.get("character_id") else None
        info.append({"n": c["position"] + 1, "who": fr._who(ch), "pron": fr._pron(ch), "line": c.get("line") or "",
                     "tone": tones.get(c.get("tone") or "", ""), "side": c.get("look") or "right",
                     "closeup": (c.get("framing") or "medium") == "closeup"})
    if not lib:
        return ollama_client.suggest_actions_all(info)
    r = ollama_client.pick_motions(info, lib)
    by_key = {m["key"]: m for m in lib}
    face_ok = [m for m in lib if m["closeup_ok"]]
    actions = {}
    for c in info:
        m = by_key.get(r["picks"].get(c["n"]))
        if m and c["closeup"] and not m["closeup_ok"]:
            m = face_ok[0] if face_ok else None  # a hand move would be outside a face close-up
        if m:
            actions[c["n"]] = {"en": m["action_en"], "ko": f"✅ {m['label']}", "key": m["key"]}
    return {"actions": actions, "sec": r["sec"], "gpu": r["gpu"]}


@router.post("/{pid}/suggest-all")
def suggest_all(pid: int):
    """🎬 모든 컷 행동 AI로 정하기: every cut's action in one gemma call, saved to the cuts (hand-written prompts and
    cuts being made are left alone)."""
    from app import comfy_client, ollama_client
    p = _require(pid)
    prefetch.touch()
    cuts = [c for c in db.list_follow_cuts(pid) if c["cast_id"] and not c["ltx_prompt"]
            and c["status"] not in ("queued", "rendering")]
    if not cuts:
        raise HTTPException(400, "정할 컷이 없어요")
    try:
        with comfy_client.foreground():
            r = _pick_actions(cuts)
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"AI가 행동을 정하지 못했어요. 다시 눌러 주세요. ({exc})") from exc
    finally:
        prefetch.touch()
    done = 0
    for c in cuts:
        a = r["actions"].get(c["position"] + 1)
        if a and a["en"] != c.get("action"):
            with db.get_conn() as conn:
                conn.execute("UPDATE follow_cuts SET action = ?, clip_path = NULL, clip_sec = NULL, status = 'pending', "
                             "error_message = NULL WHERE id = ?", (a["en"], c["id"]))
            done += 1
    prefetch.request(pid)
    return {"updated": done, "sec": r["sec"], "actions": {str(k): v for k, v in r["actions"].items()},
            "project": _detail(db.get_follow_project(pid))}


@router.post("/{pid}/refine")
def refine(pid: int, payload: RefineIn):
    """✨ AI로 정리하기: a roughly written place / action / prompt, tidied by gemma into what works (2026-10-02).
    Nothing is saved here -- the page shows the result and the viewer keeps or edits it."""
    from app import comfy_client, ollama_client
    p = _require(pid)
    text = payload.text.strip()
    if not text and payload.kind != "suggest":
        raise HTTPException(400, "정리할 내용을 먼저 적어 주세요")
    if len(text) > 1500:
        raise HTTPException(400, "1500자 이하로 적어 주세요")
    cut = db.get_follow_cut(payload.cut_id) if payload.cut_id else None
    if payload.kind in ("action", "prompt", "suggest") and (not cut or cut["project_id"] != pid):
        raise HTTPException(404, "cut not found")
    prefetch.touch()
    try:
        with comfy_client.foreground():
            if payload.kind == "background":
                return ollama_client.refine_background(text)
            cast = db.get_follow_cast(cut["cast_id"]) if cut.get("cast_id") else None
            character = db.get_character(cast["character_id"]) if cast and cast.get("character_id") else None
            if payload.kind == "suggest" and any(m["verified"] for m in db.list_motions()):
                r = _pick_actions([cut])
                a = r["actions"].get(cut["position"] + 1)
                if not a:
                    raise ollama_client.RefineError("no pick")
                return {**a, "sec": r["sec"], "gpu": r["gpu"]}
            if payload.kind == "suggest":
                cuts = db.list_follow_cuts(pid)
                i = next(k for k, x in enumerate(cuts) if x["id"] == cut["id"])
                tone = next((t.get("label") for t in fr.tone_styles() if t["key"] == (cut.get("tone") or "")), "")
                return ollama_client.suggest_action(
                    cut.get("line") or "", tone, fr._who(character), fr._pron(character),
                    (p.get("setting") or fr.DEFAULT_SETTING).split(":")[0], cut.get("look") or "right",
                    cuts[i - 1]["line"] if i > 0 else "", cuts[i + 1]["line"] if i + 1 < len(cuts) else "")
            if payload.kind == "action":
                return ollama_client.refine_action(
                    text, fr._who(character), fr._pron(character), cut.get("spoken_line") or cut.get("line") or "",
                    (p.get("setting") or fr.DEFAULT_SETTING).split(":")[0], cut.get("look") or "right")
            if payload.kind == "prompt":
                return _refine_prompt(p, cut, cast, character, text)
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"AI 정리가 실패했어요. 다시 눌러 주세요. ({exc})") from exc
    finally:
        # counted from the END too: the next ✨ usually follows within seconds, and a person picture slipped in
        # between and made it wait a whole picture (~45 s; 20 s run 197, 2026-10-06)
        prefetch.touch()
    raise HTTPException(400, "kind must be background, action or prompt")


# ---------------------------------------------------------------- 검증된 동작 · 프롬프트 기록 (2026-10-07)
@router.get("/{pid}/cuts/{cut_id}/prompts")
def cut_prompts(pid: int, cut_id: int):
    """Every prompt used for this cut of this preset (any run), with the clip it made."""
    p = _require(pid)
    c = db.get_follow_cut(cut_id)
    if not c or c["project_id"] != pid:
        raise HTTPException(404, "cut not found")
    rows = db.list_prompt_history(p.get("template_id") or p["id"], c["position"])
    return [{**r, "clip_url": _media(r["clip_path"]) if r.get("clip_path") and (MEDIA_DIR / r["clip_path"]).exists()
             else None, "current": r["prompt"] == (c.get("ltx_prompt") or "")} for r in rows]


class Star(BaseModel):
    starred: bool = True


@router.post("/prompts/{hid}/star")
def star_prompt(hid: int, payload: Star):
    r = db.star_prompt_history(hid, payload.starred)
    if not r:
        raise HTTPException(404, "기록을 찾을 수 없어요")
    return r


class UsePrompt(BaseModel):
    history_id: int


@router.post("/{pid}/cuts/{cut_id}/use-prompt")
def use_prompt(pid: int, cut_id: int, payload: UsePrompt):
    """Put an earlier prompt (and its seed, so it comes out the same way) back on this cut."""
    p = _require(pid)
    c = db.get_follow_cut(cut_id)
    h = db.get_prompt_history(payload.history_id)
    if not c or c["project_id"] != pid or not h:
        raise HTTPException(404, "not found")
    if c["status"] in ("queued", "rendering"):
        raise HTTPException(400, "만드는 중인 컷은 바꿀 수 없어요")
    with db.get_conn() as conn:
        conn.execute("UPDATE follow_cuts SET ltx_prompt = ?, render_seed = COALESCE(?, render_seed), clip_path = NULL, "
                     "clip_sec = NULL, status = 'pending', error_message = NULL WHERE id = ?",
                     (h["prompt"], h["seed"], cut_id))
    prefetch.request(pid)
    return _detail(db.get_follow_project(pid))

