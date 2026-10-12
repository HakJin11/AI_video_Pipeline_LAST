"""Render every motion_library line on one cut with one seed and store the result (2026-10-07).

usage: python scripts/test_motions.py [--closeup] [--only key,key]

Uses a private test run made from preset 5 (default cafe, cached stills), prefetch off. Before each render it waits
while any OTHER run has cuts queued/rendering, so it never pushes a viewer's render back. Each clip is scored by how
much the picture moves (mean frame difference after the first 0.3 s, compared with the same cut with no action) and
put on a contact sheet (media/motion_tests/) to be judged by eye; the verdict is then stored with --verdict.

    python scripts/test_motions.py --verdict wave=good,clap=bad:"hands cover the mouth"
"""
import argparse
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402  (leaf module: no app startup side effects)

API = "http://127.0.0.1:8010/api/follow"
OUT = ROOT / "media" / "motion_tests"  # served by the app, so the page can play the samples
FF = "C:/ffmpeg/bin/ffmpeg"
cl = httpx.Client(timeout=600)


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def others_busy(test_pid: int) -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM follow_cuts WHERE status IN ('queued','rendering') AND project_id != ? "
                            "LIMIT 1", (test_pid,)).fetchone() is not None


def motion_score(clip: Path) -> float:
    """Mean absolute difference between consecutive frames (64x36 grey), skipping the first 0.3 s."""
    raw = subprocess.run([FF, "-v", "error", "-ss", "0.3", "-i", str(clip), "-vf", "scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = 64 * 36
    frames = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    if len(frames) < 2:
        return 0.0
    tot = 0
    for a, b in zip(frames, frames[1:]):
        tot += sum(abs(x - y) for x, y in zip(a, b))
    return round(tot / (len(frames) - 1) / n, 3)


def sheet(rows: list[tuple[str, Path]], dest: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 16)
    tiles = []
    for label, clip in rows:
        dur = float(subprocess.check_output(["C:/ffmpeg/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
                                             "-of", "csv=p=0", str(clip)]).decode())
        row = Image.new("RGB", (170 + 6 * 220, 124), "white")
        ImageDraw.Draw(row).text((4, 4), label, fill="black", font=font)
        for k, t in enumerate([0.1, 0.3, 0.5, 0.65, 0.8, 0.95]):
            b = subprocess.run([FF, "-v", "error", "-ss", f"{t * dur:.3f}", "-i", str(clip), "-frames:v", "1",
                                "-vf", "scale=220:-2", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
            if b:
                row.paste(Image.open(io.BytesIO(b)), (170 + k * 220, 0))
        tiles.append(row)
    out = Image.new("RGB", (tiles[0].width, 124 * len(tiles)), "white")
    for i, t in enumerate(tiles):
        out.paste(t, (0, i * 124))
    out.save(dest)


def render(pid: int, cut: dict, action: str) -> Path:
    while others_busy(pid):
        log("다른 영상이 만들어지는 중 — 기다려요")
        time.sleep(20)
    cl.patch(f"{API}/{pid}/cuts/{cut['id']}", json={"action": action}).raise_for_status()
    cl.post(f"{API}/{pid}/cuts/{cut['id']}/render").raise_for_status()
    while True:
        time.sleep(4)
        c = next(x for x in cl.get(f"{API}/{pid}").json()["cuts"] if x["id"] == cut["id"])
        if c["status"] not in ("queued", "rendering"):
            break
    if not c.get("clip_path"):
        raise RuntimeError(f"render failed: {c.get('error_message')}")
    return ROOT / "media" / c["clip_path"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closeup", action="store_true", help="test the face motions on a close-up cut")
    ap.add_argument("--only", default="")
    ap.add_argument("--verdict", default="", help='key=good|bad[:"why"],...')
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.json"
    results = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}

    if a.verdict:
        for item in a.verdict.split(","):
            key, v = item.split("=", 1)
            verdict, _, why = v.partition(":")
            r = results.get(key.strip(), {})
            r.update(verdict=verdict.strip(), why=why.strip().strip('"'))
            results[key.strip()] = r
            db.set_motion_result(key.strip(), verdict.strip() == "good", r.get("score"),
                                 "good" if verdict.strip() == "good" else "bad", r.get("sample"))
        res_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        log("verdicts stored")
        return

    motions = db.list_motions()
    if a.only:
        motions = [m for m in motions if m["key"] in a.only.split(",")]
    if a.closeup:
        motions = [m for m in motions if m["closeup_ok"]]
    run = cl.post(f"{API}/catalog/5/start", json={"length_sec": 10}).json()
    pid = run["id"]
    cl.post(f"{API}/{pid}/prefetch", json={"on": False})
    pos = 7 if a.closeup else 2  # cut 8: A close-up "한 번만..." / cut 3: B medium "보드게임 재밌나?"
    cut = next(c for c in run["cuts"] if c["position"] == pos)
    cl.patch(f"{API}/{pid}/cuts/{cut['id']}", json={"tone": ""}).raise_for_status()  # a 말투 face would fight the motion
    log(f"test run {pid}, cut {pos + 1} ({cut['framing']}), {len(motions)} motions")
    tag = "closeup" if a.closeup else "medium"
    rows = []
    base = render(pid, cut, "")
    base_dst = OUT / f"_baseline_{tag}.mp4"
    shutil.copy(base, base_dst)
    base_score = motion_score(base_dst)
    rows.append((f"(동작 없음)\n움직임 {base_score}", base_dst))
    log("baseline", base_score)
    for m in motions:
        t0 = time.time()
        clip = render(pid, cut, m["action_en"])
        dst = OUT / f"{m['key']}_{tag}.mp4"
        shutil.copy(clip, dst)
        sc = motion_score(dst)
        results[m["key"] + ("" if not a.closeup else "_closeup")] = {"score": sc, "baseline": base_score,
                                                                     "sample": f"motion_tests/{dst.name}",
                                                                     "sec": round(time.time() - t0)}
        rows.append((f"{m['label']}\n움직임 {sc} (기본 {base_score})", dst))
        log(f"{m['key']}: motion {sc} vs {base_score} ({time.time() - t0:.0f}s)")
        res_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(0, len(rows), 7):
        sheet(rows[i:i + 7], OUT / f"sheet_{tag}_{i // 7 + 1}.png")
    log("sheets in", OUT)


if __name__ == "__main__":
    main()
