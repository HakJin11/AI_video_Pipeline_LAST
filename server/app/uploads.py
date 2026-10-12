from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.config import MEDIA_DIR


def save_upload(file: UploadFile, subdir: str) -> str:
    """Save an uploaded file under media/<subdir>/, return the path relative to MEDIA_DIR."""
    ext = Path(file.filename or "").suffix or ""
    filename = f"{uuid4().hex}{ext}"
    dest_dir = MEDIA_DIR / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    with dest.open("wb") as f:
        f.write(file.file.read())
    return f"{subdir}/{filename}"
