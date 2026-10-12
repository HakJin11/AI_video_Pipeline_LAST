from fastapi import APIRouter, HTTPException, Query

from app import db
from app.compose import ensure_composite
from app.schemas import CompositeCreate

router = APIRouter(prefix="/api/composites", tags=["composites"])


@router.get("")
def list_composites():
    return db.list_composites()


@router.get("/find")
def find_composite(
    character1_id: int = Query(...),
    character2_id: int = Query(...),
    background_id: int = Query(...),
):
    composite = db.find_composite(character1_id, character2_id, background_id)
    if not composite:
        raise HTTPException(404, "no composite registered for this combination")
    return composite


@router.post("")
def create_composite(payload: CompositeCreate):
    """Generate a new composite via ComfyUI (Qwen Image Edit) if one isn't already cached for
    this character1/character2/background combination -- lets a brand-new character (e.g. a
    freshly uploaded personal photo with no pre-made composite) go straight to a video without
    the user having to hand-run the compose workflow in ComfyUI and import the result."""
    try:
        return ensure_composite(payload.character1_id, payload.character2_id, payload.background_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
