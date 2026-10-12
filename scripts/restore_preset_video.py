"""Download a preset's reference clip from its YouTube page and cut it into the stored cuts again (2026-10-12).

The shared copy of this app does not ship the original video (it is someone else's work). Every preset keeps its
YouTube URL, start time, length and cut times in the database; this rebuilds media/follow/<id>/ref.mp4 and the
ref_cutNN.mp4/.jpg files from them. Needs yt-dlp (pip install yt-dlp) and ffmpeg.

usage: python scripts/restore_preset_video.py            (every preset that has a YouTube URL)
       python scripts/restore_preset_video.py 5          (one preset)
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402
from app import follow_render as fr  # noqa: E402
from app.config import MEDIA_DIR  # noqa: E402


def restore(pid: int) -> None:
    p = db.get_follow_project(pid)
    if not p or not p.get("source_url"):
        print(f"preset {pid}: no YouTube URL, skipped")
        return
    cuts = db.list_follow_cuts(pid)
    pdir = fr.project_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    print(f"preset {pid}: downloading {p['source_url']} ({p['source_start_sec']}s + {p['length_sec']}s)")
    with tempfile.TemporaryDirectory() as tmp:
        src = fr.download_section(p["source_url"], float(p["source_start_sec"] or 0), float(p["length_sec"]),
                                  Path(tmp))
        ref = MEDIA_DIR / (p.get("ref_video_path") or f"follow/{pid}/ref.mp4")
        ref.parent.mkdir(parents=True, exist_ok=True)
        fr.trim_reference(src, ref, 0.0, float(p["length_sec"]))  # the download already starts at the start time
    for c in cuts:
        clip = MEDIA_DIR / (c.get("ref_clip_path") or f"follow/{pid}/ref_cut{c['position']:02d}.mp4")
        thumb = MEDIA_DIR / (c.get("ref_thumb_path") or f"follow/{pid}/ref_cut{c['position']:02d}.jpg")
        fr.extract_ref_cut(ref, c["start_sec"], c["end_sec"], clip, thumb)
        db.update_follow_cut(c["id"], ref_clip_path=fr.rel(clip), ref_thumb_path=fr.rel(thumb))
    for old in pdir.glob("ref_*s.mp4"):  # 10/20/30 s copies are made again when needed
        old.unlink(missing_ok=True)
    print(f"preset {pid}: {len(cuts)} cuts ready")


def main():
    ids = [int(a) for a in sys.argv[1:]] or [p["id"] for p in db.list_follow_projects(is_template=True)
                                            if p.get("source_url")]
    if not shutil.which("ffmpeg") and not Path(fr.FFMPEG_PATH).exists():
        print("ffmpeg가 필요합니다 (README의 설치 방법 참고)")
    for pid in ids:
        restore(pid)


if __name__ == "__main__":
    main()
