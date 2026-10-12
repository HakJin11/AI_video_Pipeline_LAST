import shutil
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db
from app.config import COMFYUI_INPUT_DIR, MEDIA_DIR

router = APIRouter(prefix="/api/library-import", tags=["library_import"])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTS = {".wav", ".mp3"}


@router.get("/scan")
def scan():
    """List image/audio files sitting in ComfyUI's shared input folder, for one-click import."""
    images, audio = [], []
    for p in sorted(COMFYUI_INPUT_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() in IMAGE_EXTS:
            images.append(p.name)
        elif p.suffix.lower() in AUDIO_EXTS:
            audio.append(p.name)
    return {"images": images, "audio": audio}


class ImportCharacter(BaseModel):
    filename: str
    name: str
    description: str
    gender: str = "unspecified"
    voice_id: int | None = None


class ImportBackground(BaseModel):
    filename: str
    name: str
    description: str


class ImportVoice(BaseModel):
    filename: str
    name: str
    gender: str = "unspecified"


class ImportComposite(BaseModel):
    filename: str
    character1_id: int
    character2_id: int
    background_id: int


def copy_from_comfy_input(filename: str, subdir: str) -> str:
    src = COMFYUI_INPUT_DIR / filename
    if not src.exists():
        raise HTTPException(404, f"file not found in ComfyUI input dir: {filename}")
    dest_dir = MEDIA_DIR / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"{uuid4().hex}{src.suffix.lower()}"
    dest = dest_dir / dest_name
    shutil.copyfile(src, dest)
    return f"{subdir}/{dest_name}"


@router.post("/character")
def import_character(payload: ImportCharacter):
    if payload.voice_id is not None and not db.get_voice(payload.voice_id):
        raise HTTPException(404, "voice not found")
    image_path = copy_from_comfy_input(payload.filename, "characters")
    return db.create_character(
        payload.name, payload.description, payload.gender, image_path, payload.voice_id
    )


@router.post("/background")
def import_background(payload: ImportBackground):
    image_path = copy_from_comfy_input(payload.filename, "backgrounds")
    return db.create_background(payload.name, payload.description, image_path)


@router.post("/voice")
def import_voice(payload: ImportVoice):
    audio_path = copy_from_comfy_input(payload.filename, "voices")
    return db.create_voice(payload.name, payload.gender, audio_path)


@router.post("/composite")
def import_composite(payload: ImportComposite):
    existing = db.find_composite(payload.character1_id, payload.character2_id, payload.background_id)
    if existing:
        return {**existing, "already_existed": True}
    image_path = copy_from_comfy_input(payload.filename, "composites")
    row = db.create_composite(
        payload.character1_id, payload.character2_id, payload.background_id, image_path
    )
    return {**row, "already_existed": False}
