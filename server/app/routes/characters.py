from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import db
from app.uploads import save_upload

router = APIRouter(prefix="/api/characters", tags=["characters"])


class CharacterUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    gender: str | None = None
    voice_id: int | None = None


@router.get("")
def list_characters():
    return db.list_characters()


@router.post("")
def create_character(
    name: str = Form(...),
    description: str = Form(...),
    gender: str = Form("unspecified"),
    voice_id: int | None = Form(None),
    image: UploadFile = File(...),
):
    if voice_id is not None and not db.get_voice(voice_id):
        raise HTTPException(404, "voice not found")
    image_path = save_upload(image, "characters")
    return db.create_character(name, description, gender, image_path, voice_id)


@router.patch("/{character_id}")
def update_character(character_id: int, payload: CharacterUpdate):
    if not db.get_character(character_id):
        raise HTTPException(404, "character not found")
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if fields.get("voice_id") is not None and not db.get_voice(fields["voice_id"]):
        raise HTTPException(404, "voice not found")
    return db.update_character(character_id, **fields)


@router.delete("/{character_id}")
def delete_character(character_id: int):
    if not db.get_character(character_id):
        raise HTTPException(404, "character not found")
    db.delete_character(character_id)
    return {"ok": True}
