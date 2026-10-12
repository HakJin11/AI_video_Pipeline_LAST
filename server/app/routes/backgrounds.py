from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import db
from app.uploads import save_upload

router = APIRouter(prefix="/api/backgrounds", tags=["backgrounds"])


@router.get("")
def list_backgrounds():
    return db.list_backgrounds()


@router.post("")
def create_background(
    name: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(...),
):
    image_path = save_upload(image, "backgrounds")
    return db.create_background(name, description, image_path)


@router.delete("/{background_id}")
def delete_background(background_id: int):
    if not db.get_background(background_id):
        raise HTTPException(404, "background not found")
    db.delete_background(background_id)
    return {"ok": True}
