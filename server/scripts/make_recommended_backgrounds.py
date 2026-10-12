"""⭐ recommended places for the 배경 panel: drawn once on this PC with Z-Image-Turbo (free) from prompts
written for it -- a place, 3-5 visible things, light/time, no people, no text. Run while nothing renders:
.venv/Scripts/python.exe scripts/make_recommended_backgrounds.py
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, follow_render as fr, local_images  # noqa: E402

PLACES = [
    ("편의점", "비 오는 저녁 편의점 안. 계산대와 컵라면 진열대, 음료 냉장고, 창밖에 젖은 거리 불빛, 하얀 형광등",
     "inside a small Korean convenience store on a rainy evening: the checkout counter, shelves of cup noodles and snacks, "
     "glass-door drink fridges glowing, wet street lights reflected in the front window, cool white fluorescent lighting"),
    ("한강 공원", "해 질 녘 한강 공원 잔디밭. 멀리 다리와 강, 돗자리와 편의점 봉지, 따뜻한 노을빛",
     "a grassy lawn in a riverside city park at sunset: a long bridge and the wide river in the distance, a picnic mat "
     "with convenience-store bags, city towers on the far bank, warm golden sunset light"),
    ("대학교 강의실", "오후의 대학교 강의실. 계단식 책상, 앞쪽 칠판과 큰 스크린, 창으로 들어오는 햇빛",
     "a university lecture hall in the afternoon: rows of tiered wooden desks, a large blackboard and projector screen "
     "at the front, tall windows letting in soft afternoon sunlight"),
    ("코인노래방", "좁은 코인노래방 방 안. 작은 소파, 벽의 큰 모니터, 탬버린과 마이크, 보라·파랑 네온 조명",
     "inside a small coin karaoke booth: a short leather sofa, a big karaoke screen on the wall, a tambourine and "
     "microphones on a little table, purple and blue neon lighting on patterned wallpaper"),
    ("PC방", "밤의 PC방. 줄지어 놓인 게이밍 의자와 모니터, 키보드 불빛, 어두운 실내와 파란 조명",
     "an internet cafe (PC bang) at night: long rows of gaming chairs facing glowing monitors, RGB keyboards, a dark "
     "room lit by blue ceiling strips and screen light"),
    ("분식집", "점심때 분식집 안. 떡볶이와 김밥이 있는 스테인리스 조리대, 작은 테이블과 둥근 의자, 따뜻한 조명",
     "inside a small Korean snack restaurant at lunchtime: a stainless-steel counter with a pan of tteokbokki and rolls "
     "of gimbap, small tables with round stools, warm cosy lighting"),
    ("학교 교실", "방과 후 텅 빈 고등학교 교실. 줄 맞춘 책상과 의자, 초록 칠판, 창밖 노을빛",
     "an empty high-school classroom after school: neat rows of desks and chairs, a green chalkboard, a wall clock, "
     "orange late-afternoon sunlight streaming through the windows"),
    ("회사 탕비실", "회사 탕비실. 커피머신과 정수기, 작은 테이블, 간식 바구니, 밝은 형광등",
     "an office pantry: a coffee machine and a water dispenser on the counter, a small round table, a basket of "
     "snacks, a fridge, bright fluorescent lighting"),
]


def main():
    db.init_db()
    have = {b["name"] for b in db.list_backgrounds() if b.get("recommended")}
    for name, ko, en in PLACES:
        if name in have:
            print("skip", name); continue
        path = local_images.generate_background(en)
        b = db.create_background(name, en, fr.rel(path), ko, "local")
        with db.get_conn() as conn:
            conn.execute("UPDATE backgrounds SET is_example = 1, recommended = 1 WHERE id = ?", (b["id"],))
        print("made", name, path.name, flush=True)


if __name__ == "__main__":
    main()
