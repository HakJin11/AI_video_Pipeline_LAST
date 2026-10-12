from fastapi import APIRouter, HTTPException

from app import db, ollama_client
from app.config import MANUAL_DIALOGUE_KEYWORD
from app.ltx_prompt import build_prompt
from app.schemas import DialogueCreate, DialogueManualCreate, DialogueReplyRequest, DialogueUpdate

router = APIRouter(prefix="/api/dialogues", tags=["dialogues"])

KEYWORD_PRESETS = ["사랑", "결혼", "싸움", "이별", "화해", "고백", "재회", "오해"]


@router.get("")
def list_dialogues():
    return db.list_dialogues()


@router.get("/keyword-presets")
def keyword_presets():
    return KEYWORD_PRESETS


def _maybe_build_ltx_prompt(
    keyword: str, char1: dict, char2: dict, background_id: int | None, line1: str, line2: str
) -> str | None:
    """Once both lines exist and a background is picked, write the LTX prompt right away (with
    real character/background context from the DB) instead of waiting until video generation.
    Timing is estimated from line length since real TTS audio doesn't exist yet — that's fine,
    actual lip-sync is driven by the audio conditioning, not the prompt's stated seconds."""
    if background_id is None or not line1 or not line2:
        return None
    bg = db.get_background(background_id)
    if not bg:
        return None
    t1 = ollama_client.estimate_seconds(line1)
    t2 = ollama_client.estimate_seconds(line2)
    return build_prompt(keyword, char1, char2, bg, line1, line2, t1, t2)


def _voice_pace(voice_id: int | None) -> float | None:
    if voice_id is None:
        return None
    voice = db.get_voice(voice_id)
    if not voice or not voice.get("observed_chars_per_sec") or not voice.get("observed_sample_count"):
        return None
    return voice["observed_chars_per_sec"]


@router.post("")
def create_dialogue(payload: DialogueCreate):
    """Generate only 인물1's opening line for the keyword. 인물2 replies later via /reply."""
    char1 = db.get_character(payload.character1_id)
    char2 = db.get_character(payload.character2_id)
    if not (char1 and char2):
        raise HTTPException(404, "character not found")

    try:
        line1 = ollama_client.generate_opening_line(
            payload.keyword,
            char1["name"],
            char1["description"],
            char2["name"],
            char2["description"],
            payload.target_duration_sec,
            _voice_pace(payload.voice1_id),
        )
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"Ollama dialogue generation failed: {exc}") from exc

    return db.create_dialogue(payload.keyword, payload.character1_id, payload.character2_id, line1, "")


@router.post("/manual")
def create_manual_dialogue(payload: DialogueManualCreate):
    if not (db.get_character(payload.character1_id) and db.get_character(payload.character2_id)):
        raise HTTPException(404, "character not found")
    return db.create_dialogue(
        MANUAL_DIALOGUE_KEYWORD, payload.character1_id, payload.character2_id, payload.line1, payload.line2
    )


@router.post("/{dialogue_id}/reply")
def generate_reply(dialogue_id: int, payload: DialogueReplyRequest):
    """Generate 인물2's line as a natural reply to the (possibly user-edited) 인물1 line."""
    dialogue = db.get_dialogue(dialogue_id)
    if not dialogue:
        raise HTTPException(404, "dialogue not found")
    char1 = db.get_character(dialogue["character1_id"])
    char2 = db.get_character(dialogue["character2_id"])

    try:
        line2 = ollama_client.generate_reply_line(
            dialogue["keyword"],
            char2["name"],
            char2["description"],
            char1["name"],
            char1["description"],
            payload.line1,
            payload.target_duration_sec,
            _voice_pace(payload.voice2_id),
        )
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"Ollama dialogue generation failed: {exc}") from exc

    row = db.update_dialogue_lines(dialogue_id, payload.line1, line2)
    ltx_prompt = _maybe_build_ltx_prompt(
        dialogue["keyword"], char1, char2, payload.background_id, payload.line1, line2
    )
    return {**row, "ltx_prompt": ltx_prompt}


@router.patch("/{dialogue_id}")
def update_dialogue(dialogue_id: int, payload: DialogueUpdate):
    dialogue = db.get_dialogue(dialogue_id)
    if not dialogue:
        raise HTTPException(404, "dialogue not found")

    row = db.update_dialogue_lines(dialogue_id, payload.line1, payload.line2)
    char1 = db.get_character(dialogue["character1_id"])
    char2 = db.get_character(dialogue["character2_id"])
    ltx_prompt = _maybe_build_ltx_prompt(
        dialogue["keyword"], char1, char2, payload.background_id, payload.line1, payload.line2
    )
    return {**row, "ltx_prompt": ltx_prompt}
