import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app import comfy_client, db, jobs, ollama_client
from app.config import MEDIA_DIR, VIDEOS_DIR
from app.ltx_prompt import build_prompt
from app.routes.voice_lines import generate_voice_line
from app.scene_render import build_dialogue_track, composite_source_image, fit_two_lines, run_ltx_clip
from app.schemas import VideoCreate

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("")
def list_videos():
    return db.list_videos()


def _run_video_generation(
    video_id: int,
    composite: dict,
    char1: dict,
    char2: dict,
    bg: dict,
    line1: str,
    line2: str,
    voice_line1: dict,
    voice_line2: dict,
    t1: int,
    t2: int,
    duration_sec: int,
    situation: str | None,
    prebuilt_prompt: str | None,
) -> dict:
    try:
        db.update_video_status(video_id, "running")

        # reuse the prompt gemma already wrote alongside the dialogue if we have one and it still
        # matches the current lines; otherwise build fresh (manual dialogues, post-cache edits, ...)
        if prebuilt_prompt and line1 in prebuilt_prompt and line2 in prebuilt_prompt:
            prompt_text = prebuilt_prompt
        else:
            prompt_text = build_prompt(situation, char1, char2, bg, line1, line2, t1, t2)

        dest = VIDEOS_DIR / f"{video_id}_{uuid4().hex}.mp4"
        track = VIDEOS_DIR / f"track_{video_id}_{uuid4().hex}.mp3"
        build_dialogue_track(
            MEDIA_DIR / voice_line1["audio_path"],
            MEDIA_DIR / voice_line2["audio_path"],
            track,
        )
        run_ltx_clip(
            composite_source_image(composite, char1["id"]),
            track, prompt_text, duration_sec, dest,
        )
        track.unlink(missing_ok=True)

        db.update_video_status(video_id, "done", video_path=f"videos/{dest.name}")
        return db.get_video(video_id)
    except Exception as exc:  # noqa: BLE001 - persist failure for the polling client
        db.update_video_status(video_id, "error", error_message=str(exc))
        raise


@router.post("")
async def create_video(payload: VideoCreate):
    composite = db.get_composite(payload.composite_id)
    dialogue = db.get_dialogue(payload.dialogue_id)
    if not (composite and dialogue):
        raise HTTPException(404, "composite or dialogue not found")

    # use the dialogue's own character1/2 (matches line1/line2 and voice1/voice2 ordering) --
    # NOT the composite's, since composite lookup is order-independent
    char1 = db.get_character(dialogue["character1_id"])
    char2 = db.get_character(dialogue["character2_id"])
    bg = db.get_background(composite["background_id"])

    voice_line1 = await asyncio.to_thread(generate_voice_line, dialogue["line1"], payload.voice1_id)
    voice_line2 = await asyncio.to_thread(generate_voice_line, dialogue["line2"], payload.voice2_id)

    voice_line1, voice_line2, t1, t2, duration_sec = await asyncio.to_thread(
        fit_two_lines, voice_line1, voice_line2, payload.target_duration_sec
    )

    # The prebuilt LTX prompt bakes in per-speaker timing that was only a character-count estimate.
    # If the real synthesized speech landed more than ~2s off for either line, rebuild it with the
    # measured t1/t2 so its "First ~N seconds / Remaining ~N seconds" markers point at the right
    # speaker mid-clip.
    prebuilt_prompt = payload.ltx_prompt
    if prebuilt_prompt:
        est1 = ollama_client.estimate_seconds(dialogue["line1"])
        est2 = ollama_client.estimate_seconds(dialogue["line2"])
        if abs(est1 - t1) > 2 or abs(est2 - t2) > 2:
            prebuilt_prompt = await asyncio.to_thread(
                build_prompt, dialogue["keyword"], char1, char2, bg,
                dialogue["line1"], dialogue["line2"], t1, t2,
            )

    video_row = await asyncio.to_thread(
        db.create_video,
        payload.composite_id,
        payload.dialogue_id,
        voice_line1["id"],
        voice_line2["id"],
        duration_sec,
    )

    job_id = jobs.start_job(
        _run_video_generation,
        video_row["id"],
        composite,
        char1,
        char2,
        bg,
        dialogue["line1"],
        dialogue["line2"],
        voice_line1,
        voice_line2,
        t1,
        t2,
        duration_sec,
        dialogue["keyword"],
        prebuilt_prompt,
    )
    return {"video_id": video_row["id"], "job_id": job_id, "duration_sec": duration_sec}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job
