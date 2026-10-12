import httpx
from fastapi import APIRouter

from app import comfy_client
from app.config import OLLAMA_URL

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health():
    comfy_ok = False
    ollama_ok = False
    try:
        # /system_stats answers 500 on this ComfyUI build ("Error handling request" in its log),
        # which made the app report ComfyUI as down while it was happily rendering. /queue is the
        # endpoint we actually depend on anyway.
        comfy_ok = comfy_client.resolve_url() is not None
    except httpx.HTTPError:
        pass
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        ollama_ok = r.status_code == 200
    except httpx.HTTPError:
        pass
    return {"comfyui": comfy_ok, "ollama": ollama_ok}
