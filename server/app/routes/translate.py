"""번역기: 한국어 <-> 영어 (프롬프트 쓰기용)."""
import re

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import db, ollama_client

router = APIRouter(prefix="/api/translate", tags=["translate"])


class TranslateIn(BaseModel):
    text: str
    target: str = "auto"  # "en" | "ko" | "auto" (Hangul in the text -> English, otherwise -> Korean)


@router.post("")
async def translate(payload: TranslateIn):
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "번역할 내용을 입력하세요")
    if len(text) > 4000:
        raise HTTPException(400, "4000자 이하로 나눠서 번역하세요")
    target = payload.target
    if target not in ("en", "ko"):
        target = "en" if re.search(r"[\uac00-\ud7a3]", text) else "ko"
    cached = db.get_translation(text, "tr_" + target)
    if cached:
        return {"target": target, "text": cached, "cached": True}
    try:
        out = await run_in_threadpool(ollama_client.translate_prompt_text, text, target)
    except ollama_client.OllamaError as exc:
        raise HTTPException(502, f"번역 실패: {exc}") from exc
    db.put_translation(text, "tr_" + target, out)
    return {"target": target, "text": out, "cached": False}
