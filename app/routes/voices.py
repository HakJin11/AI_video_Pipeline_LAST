import subprocess
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import db
from app.config import FFMPEG_PATH, MEDIA_DIR
from app.uploads import save_upload

router = APIRouter(prefix="/api/voices", tags=["voices"])


@router.get("")
def list_voices():
    return db.list_voices()


# The bank voices that clone cleanly are all short (~3.6s). A longer, rawer personal reference
# (phone recordings run 7s+) makes Qwen3 TTS both far slower per call and carry over the
# reference's own slower pacing into every line it generates -- so measured, real fixes, not a
# guess. Cap every reference to this length regardless of source.
_MAX_REFERENCE_SEC = 5.0


def _transcode_to_wav(rel_path: str) -> str:
    """Normalize every uploaded/recorded reference to a short mono wav: browser mic recordings
    come in as webm/opus and phone voice memos as m4a/aac, neither reliably supported by
    ComfyUI's audio loader, and anything longer than _MAX_REFERENCE_SEC gets trimmed down to
    match the pacing profile of the voices that are known to clone well."""
    src = MEDIA_DIR / rel_path
    dest = src if src.suffix.lower() == ".wav" else src.with_suffix(".wav")
    tmp = dest.with_name(f"_tmp_{dest.name}")
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", str(src), "-t", str(_MAX_REFERENCE_SEC), "-ar", "24000", "-ac", "1", str(tmp)],
        check=True, capture_output=True,
    )
    if src != dest:
        src.unlink(missing_ok=True)
    tmp.replace(dest)
    return str(Path(rel_path).with_suffix(".wav")).replace("\\", "/")


@router.post("")
def create_voice(
    name: str = Form(...),
    gender: str = Form("unspecified"),
    audio: UploadFile = File(...),
):
    audio_path = save_upload(audio, "voices")
    try:
        audio_path = _transcode_to_wav(audio_path)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(400, f"오디오 파일을 처리할 수 없습니다: {exc}") from exc
    return db.create_voice(name, gender, audio_path)


@router.delete("/{voice_id}")
def delete_voice(voice_id: int):
    if not db.get_voice(voice_id):
        raise HTTPException(404, "voice not found")
    db.delete_voice(voice_id)
    return {"ok": True}
