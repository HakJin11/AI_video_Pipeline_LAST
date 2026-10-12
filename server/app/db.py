import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    gender TEXT NOT NULL DEFAULT 'unspecified',
    image_path TEXT NOT NULL,
    voice_id INTEGER REFERENCES voices(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backgrounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    image_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS composites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character1_id INTEGER NOT NULL REFERENCES characters(id),
    character2_id INTEGER NOT NULL REFERENCES characters(id),
    background_id INTEGER NOT NULL REFERENCES backgrounds(id),
    image_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(character1_id, character2_id, background_id)
);

CREATE TABLE IF NOT EXISTS voices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    gender TEXT NOT NULL DEFAULT 'unspecified',
    reference_audio_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dialogues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    character1_id INTEGER NOT NULL REFERENCES characters(id),
    character2_id INTEGER NOT NULL REFERENCES characters(id),
    line1 TEXT NOT NULL,
    line2 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    voice_id INTEGER NOT NULL REFERENCES voices(id),
    audio_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(text, voice_id)
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    composite_id INTEGER NOT NULL REFERENCES composites(id),
    dialogue_id INTEGER NOT NULL REFERENCES dialogues(id),
    voice_line1_id INTEGER NOT NULL REFERENCES voice_lines(id),
    voice_line2_id INTEGER NOT NULL REFERENCES voice_lines(id),
    duration_sec INTEGER NOT NULL DEFAULT 20,
    video_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    premise TEXT NOT NULL,
    character1_id INTEGER NOT NULL REFERENCES characters(id),
    character2_id INTEGER NOT NULL REFERENCES characters(id),
    voice1_id INTEGER REFERENCES voices(id),
    voice2_id INTEGER REFERENCES voices(id),
    scene_count INTEGER NOT NULL DEFAULT 6,
    target_total_sec INTEGER NOT NULL DEFAULT 80,
    fast_preview INTEGER NOT NULL DEFAULT 1,
    music_path TEXT,
    music_gain REAL NOT NULL DEFAULT 0.25,
    intro_text TEXT,
    outro_text TEXT,
    final_video_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    background_id INTEGER NOT NULL REFERENCES backgrounds(id),
    situation TEXT NOT NULL DEFAULT '',
    line1 TEXT NOT NULL DEFAULT '',
    line2 TEXT NOT NULL DEFAULT '',
    caption TEXT,
    transition TEXT NOT NULL DEFAULT 'fade',
    composite_id INTEGER REFERENCES composites(id),
    voice_line1_id INTEGER REFERENCES voice_lines(id),
    voice_line2_id INTEGER REFERENCES voice_lines(id),
    clip_path TEXT,
    ltx_prompt TEXT,
    lines_by_user INTEGER NOT NULL DEFAULT 0,
    render_sec REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS follow_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    ref_video_path TEXT NOT NULL,
    source_start_sec REAL NOT NULL DEFAULT 0,
    length_sec REAL NOT NULL DEFAULT 10,
    setting TEXT NOT NULL DEFAULT '',
    background_id INTEGER REFERENCES backgrounds(id),
    fast_preview INTEGER NOT NULL DEFAULT 1,
    final_video_path TEXT,
    compare_video_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_cast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES follow_projects(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    character_id INTEGER REFERENCES characters(id),
    voice_id INTEGER REFERENCES voices(id),
    costume TEXT NOT NULL DEFAULT '',
    still_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_styles (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    instruct TEXT NOT NULL,          -- what Qwen3-TTS is told about the delivery
    sample_text TEXT NOT NULL,       -- what the sample clip of this tone says
    face TEXT NOT NULL DEFAULT '',   -- how the face is described in the video prompt
    respell TEXT NOT NULL DEFAULT '',-- how gemma re-spells a line for voices without tone clips
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS voice_tones (
    voice_id INTEGER NOT NULL REFERENCES voices(id),
    tone TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    reference_text TEXT NOT NULL,
    PRIMARY KEY (voice_id, tone)
);

CREATE TABLE IF NOT EXISTS follow_stills (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- a finished cut clip, kept by a hash of everything that went into it (still, voice track, prompt, seed,
-- length, guide, quality): the same cut in another run -- or made ahead of time -- is reused instantly
CREATE TABLE IF NOT EXISTS follow_clip_cache (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    clip_sec REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_translations (
    src TEXT NOT NULL,
    lang TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, lang)
);

-- 2026-10-07: tested acting lines for the 행동 field. Each was rendered on the same cut/seed and checked for visible
-- movement, mouth in view and the face kept toward the camera; only "verified" ones are offered as ✅ chips.
CREATE TABLE IF NOT EXISTS motion_library (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    action_en TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'hands',
    closeup_ok INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    motion_score REAL,
    result TEXT NOT NULL DEFAULT '',
    sample_path TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- 2026-10-07: every prompt actually sent to LTX for a cut, with its seed and the clip it made, so a good one can be
-- found and used again (the automatic prompt was rebuilt each time and never kept).
CREATE TABLE IF NOT EXISTS prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    cut_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    template_id INTEGER,
    prompt TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    motion TEXT NOT NULL DEFAULT '',
    framing TEXT NOT NULL DEFAULT '',
    seed INTEGER,
    clip_path TEXT,
    from_cache INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prompt_history_cut ON prompt_history (template_id, position);

CREATE TABLE IF NOT EXISTS follow_cuts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES follow_projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    ref_clip_path TEXT,
    ref_thumb_path TEXT,
    cast_id INTEGER REFERENCES follow_cast(id) ON DELETE SET NULL,
    look TEXT NOT NULL DEFAULT 'right',
    line TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    ltx_prompt TEXT,
    clip_path TEXT,
    clip_sec REAL,
    render_sec REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Schema + migrations only -- idempotent and safe from any process (scripts, checks)."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def recover_stale_renders() -> list[int]:
    """Called once by the web server's startup hook, never at import. It rewrites live render
    statuses, so running it from a second process (anything that imported app.main) reset the
    running server's 렌더 중/대기 scenes to 렌더 안 됨 in the middle of a render.

    Returns the 따라하기 projects whose render was cut off, so the server can pick them up again."""
    with get_conn() as conn:
        interrupted = [r[0] for r in conn.execute(
            "SELECT DISTINCT project_id FROM follow_cuts WHERE status IN ('rendering', 'queued') "
            "AND clip_path IS NULL").fetchall()]
        _recover_stale_renders(conn)
    return interrupted


def _recover_stale_renders(conn: sqlite3.Connection) -> None:
    """Render jobs live only in this process's memory, so anything still marked 'rendering' when we
    boot belongs to a run that died with the previous process -- it will never finish or update
    itself, and the UI would show a spinner forever. A scene that got as far as writing its clip is
    really done; the rest go back to pending so they can simply be re-rendered."""
    conn.execute(
        "UPDATE story_scenes SET status = 'done' WHERE status IN ('rendering', 'queued') AND clip_path IS NOT NULL"
    )
    conn.execute(
        "UPDATE story_scenes SET status = 'pending', error_message = '이전 실행이 중단되어 렌더가 끝나지 않았습니다' "
        "WHERE status IN ('rendering', 'queued')"
    )
    # a scene that has a finished clip is done, whatever a crashed duplicate render wrote over it
    conn.execute(
        "UPDATE story_scenes SET status = 'done', error_message = NULL "
        "WHERE status = 'error' AND clip_path IS NOT NULL AND error_message LIKE 'UNIQUE constraint failed%'"
    )
    conn.execute("UPDATE stories SET status = 'outlined' WHERE status IN ('rendering', 'outlining')")
    conn.execute(
        "UPDATE follow_cuts SET status = CASE WHEN clip_path IS NOT NULL THEN 'done' ELSE 'pending' END "
        "WHERE status IN ('rendering', 'queued')"
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """SQLite has no 'ADD COLUMN IF NOT EXISTS', so guard manually for columns added after the
    table already existed on disk."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(voices)")}
    if "observed_chars_per_sec" not in cols:
        conn.execute("ALTER TABLE voices ADD COLUMN observed_chars_per_sec REAL")
    if "observed_sample_count" not in cols:
        conn.execute("ALTER TABLE voices ADD COLUMN observed_sample_count INTEGER NOT NULL DEFAULT 0")
    if "reference_text" not in cols:  # what the reference clip says: cloning with it sounds more natural
        conn.execute("ALTER TABLE voices ADD COLUMN reference_text TEXT")
    if "hidden" not in cols:  # taken out of the pickers; kept so finished videos can still re-render
        conn.execute("ALTER TABLE voices ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    if "design_instruct" not in cols:  # how this voice was described to VoiceDesign (makes new tone clips)
        conn.execute("ALTER TABLE voices ADD COLUMN design_instruct TEXT")

    style_cols = {row["name"] for row in conn.execute("PRAGMA table_info(voice_styles)")}
    if style_cols and "respell" not in style_cols:
        conn.execute("ALTER TABLE voice_styles ADD COLUMN respell TEXT NOT NULL DEFAULT ''")

    char_cols = {row["name"] for row in conn.execute("PRAGMA table_info(characters)")}
    if "voice_id" not in char_cols:
        conn.execute("ALTER TABLE characters ADD COLUMN voice_id INTEGER REFERENCES voices(id)")

    story_cols = {row["name"] for row in conn.execute("PRAGMA table_info(stories)")}
    if "fast_preview" not in story_cols:
        conn.execute("ALTER TABLE stories ADD COLUMN fast_preview INTEGER NOT NULL DEFAULT 1")

    bg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(backgrounds)")}
    if bg_cols and "prompt_ko" not in bg_cols:
        # the place prompt in Korean next to the English one (description), shown to viewers as examples
        conn.execute("ALTER TABLE backgrounds ADD COLUMN prompt_ko TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE backgrounds ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'")
        conn.execute("ALTER TABLE backgrounds ADD COLUMN is_example INTEGER NOT NULL DEFAULT 0")
    if bg_cols and "recommended" not in bg_cols:
        # ⭐ backgrounds drawn by us with Z-Image from a tuned prompt, listed first in the 배경 panel
        conn.execute("ALTER TABLE backgrounds ADD COLUMN recommended INTEGER NOT NULL DEFAULT 0")

    cut_cols = {row["name"] for row in conn.execute("PRAGMA table_info(follow_cuts)")}
    if "render_seed" not in cut_cols:
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN render_seed INTEGER")
    conn.execute("UPDATE follow_cuts SET render_seed = (random() & 281474976710655) WHERE render_seed IS NULL")
    if cut_cols and "people" not in cut_cols:
        # how many people the ORIGINAL cut shows (detected), and who plays the second one
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN people INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN cast_id2 INTEGER REFERENCES follow_cast(id)")
    if cut_cols and "framing" not in cut_cols:
        # how the original cut is shot: size of the shot and what the camera does
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN framing TEXT NOT NULL DEFAULT 'medium'")
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN camera TEXT NOT NULL DEFAULT 'locked'")
    if cut_cols and "spoken_line" not in cut_cols:
        # the line as it is actually fed to the TTS (shortened to fit, re-spelled for the tone)
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN spoken_line TEXT")
    if cut_cols and "timing" not in cut_cols:
        # {"still": s, "voice": s, "video": s, "join": s} -- so the page can say WHERE the time went
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN timing TEXT")
    if cut_cols and "tone" not in cut_cols:
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN tone TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE follow_cuts ADD COLUMN motion TEXT NOT NULL DEFAULT ''")

    fp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(follow_projects)")}
    if "match_reference" not in fp_cols:
        conn.execute("ALTER TABLE follow_projects ADD COLUMN match_reference INTEGER NOT NULL DEFAULT 0")
    if fp_cols and "last_render_sec" not in fp_cols:
        # wall-clock seconds of the last "전부 만들기" (voices + stills + clips), what the user waited
        conn.execute("ALTER TABLE follow_projects ADD COLUMN last_render_sec REAL")
    if fp_cols and "use_still" not in fp_cols:
        # 1 = make a styled still first (Qwen), 0 = feed the character photo straight to LTX
        conn.execute("ALTER TABLE follow_projects ADD COLUMN use_still INTEGER NOT NULL DEFAULT 1")

    follow_cols = {row["name"] for row in conn.execute("PRAGMA table_info(follow_projects)")}
    if "is_template" not in follow_cols:
        # 1 = an admin-made preset shown in 영상 선택하기; 0 = a viewer's own run copied from one
        conn.execute("ALTER TABLE follow_projects ADD COLUMN is_template INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE follow_projects ADD COLUMN published INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE follow_projects ADD COLUMN template_id INTEGER")
        conn.execute("ALTER TABLE follow_projects ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    scene_cols = {row["name"] for row in conn.execute("PRAGMA table_info(story_scenes)")}
    if "render_sec" not in scene_cols:
        conn.execute("ALTER TABLE story_scenes ADD COLUMN render_sec REAL")
    if "ltx_prompt" not in scene_cols:
        conn.execute("ALTER TABLE story_scenes ADD COLUMN ltx_prompt TEXT")
    if "lines_by_user" not in scene_cols:
        # 1 = the user typed these lines themselves -> the renderer must never shorten them
        conn.execute("ALTER TABLE story_scenes ADD COLUMN lines_by_user INTEGER NOT NULL DEFAULT 0")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------- characters ----------

_CHARACTER_UPDATABLE = {"name", "description", "gender", "voice_id"}


def create_character(name: str, description: str, gender: str, image_path: str,
                     voice_id: int | None = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO characters (name, description, gender, image_path, voice_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, gender, image_path, voice_id, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM characters WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_character(character_id: int, **fields) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in _CHARACTER_UPDATABLE}
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with get_conn() as conn:
            conn.execute(f"UPDATE characters SET {cols} WHERE id = ?", (*updates.values(), character_id))
    return get_character(character_id)


def list_characters() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM characters ORDER BY id DESC").fetchall())


def get_character(character_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone())


def delete_character(character_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))


# ---------- backgrounds ----------

def create_background(name: str, description: str, image_path: str, prompt_ko: str = "",
                      source: str = "upload") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO backgrounds (name, description, image_path, created_at, prompt_ko, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, image_path, now_iso(), prompt_ko, source),
        )
        return row_to_dict(conn.execute("SELECT * FROM backgrounds WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_backgrounds() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM backgrounds ORDER BY id DESC").fetchall())


def get_background(background_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM backgrounds WHERE id = ?", (background_id,)).fetchone())


def delete_background(background_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM backgrounds WHERE id = ?", (background_id,))


# ---------- composites ----------

def find_composite(character1_id: int, character2_id: int, background_id: int) -> dict | None:
    """Match the character pair regardless of which slot each one is in, so swapping who is
    인물1 vs 인물2 still finds a composite already registered with the pair in the other order."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM composites WHERE background_id = ? AND "
            "((character1_id = ? AND character2_id = ?) OR (character1_id = ? AND character2_id = ?))",
            (background_id, character1_id, character2_id, character2_id, character1_id),
        ).fetchone()
        return row_to_dict(row)


def create_composite(character1_id: int, character2_id: int, background_id: int, image_path: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO composites (character1_id, character2_id, background_id, image_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (character1_id, character2_id, background_id, image_path, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM composites WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_composites() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM composites ORDER BY id DESC").fetchall())


def get_composite(composite_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM composites WHERE id = ?", (composite_id,)).fetchone())


# ---------- voices ----------

def create_voice(name: str, gender: str, reference_audio_path: str, reference_text: str | None = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO voices (name, gender, reference_audio_path, reference_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, gender, reference_audio_path, reference_text, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM voices WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_voices() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM voices WHERE hidden = 0 ORDER BY id DESC").fetchall())


def get_voice(voice_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM voices WHERE id = ?", (voice_id,)).fetchone())


def delete_voice(voice_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))


def record_voice_pace(voice_id: int, chars_per_sec: float) -> None:
    """Track each voice's real observed speaking pace (chars of Korean text per second of
    synthesized audio) as an exponential moving average -- some cloned voices (e.g. a personal
    recording) run meaningfully slower than the bank voices the global pacing constant was
    calibrated on, so dialogue length targeting needs a per-voice number, not one global guess."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT observed_chars_per_sec, observed_sample_count FROM voices WHERE id = ?", (voice_id,)
        ).fetchone()
        if row is None:
            return
        prev, n = row["observed_chars_per_sec"], row["observed_sample_count"] or 0
        new_val = chars_per_sec if prev is None else (prev * 0.7 + chars_per_sec * 0.3)
        conn.execute(
            "UPDATE voices SET observed_chars_per_sec = ?, observed_sample_count = ? WHERE id = ?",
            (new_val, n + 1, voice_id),
        )


# ---------- dialogues ----------

def create_dialogue(keyword: str, character1_id: int, character2_id: int, line1: str, line2: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dialogues (keyword, character1_id, character2_id, line1, line2, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (keyword, character1_id, character2_id, line1, line2, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM dialogues WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_dialogue_lines(dialogue_id: int, line1: str, line2: str) -> dict | None:
    with get_conn() as conn:
        conn.execute("UPDATE dialogues SET line1 = ?, line2 = ? WHERE id = ?", (line1, line2, dialogue_id))
        return row_to_dict(conn.execute("SELECT * FROM dialogues WHERE id = ?", (dialogue_id,)).fetchone())


def get_dialogue(dialogue_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM dialogues WHERE id = ?", (dialogue_id,)).fetchone())


def list_dialogues() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM dialogues ORDER BY id DESC").fetchall())


# ---------- voice_lines ----------

def find_voice_line(text: str, voice_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM voice_lines WHERE text = ? AND voice_id = ?", (text, voice_id)
        ).fetchone()
        return row_to_dict(row)


def create_voice_line(text: str, voice_id: int, audio_path: str) -> dict:
    """Insert, or return the row that already exists for (text, voice_id). Two renders that need
    the same line can both miss the cache and both synthesize it; the second must not crash the
    scene with a UNIQUE violation -- it just reuses the first one's audio."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO voice_lines (text, voice_id, audio_path, created_at) VALUES (?, ?, ?, ?)",
            (text, voice_id, audio_path, now_iso()),
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM voice_lines WHERE text = ? AND voice_id = ?", (text, voice_id)
        ).fetchone())


def get_voice_line(voice_line_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM voice_lines WHERE id = ?", (voice_line_id,)).fetchone())


# ---------- videos ----------

def create_video(composite_id: int, dialogue_id: int, voice_line1_id: int, voice_line2_id: int,
                  duration_sec: int) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (composite_id, dialogue_id, voice_line1_id, voice_line2_id, duration_sec, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (composite_id, dialogue_id, voice_line1_id, voice_line2_id, duration_sec, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM videos WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_video_status(video_id: int, status: str, video_path: str | None = None,
                         error_message: str | None = None) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET status = ?, video_path = COALESCE(?, video_path), error_message = ? WHERE id = ?",
            (status, video_path, error_message, video_id),
        )
        return row_to_dict(conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())


def get_video(video_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())


def list_videos() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM videos ORDER BY id DESC").fetchall())


# ---------- stories ----------

_STORY_UPDATABLE = {
    "title", "premise", "target_total_sec", "fast_preview", "music_path", "music_gain",
    "intro_text", "outro_text", "final_video_path", "status", "error_message",
}
_SCENE_UPDATABLE = {
    "position", "background_id", "situation", "line1", "line2", "caption", "transition",
    "composite_id", "voice_line1_id", "voice_line2_id", "clip_path", "ltx_prompt", "lines_by_user",
    "render_sec",
    "status", "error_message",
}


def create_story(premise: str, character1_id: int, character2_id: int, voice1_id: int | None,
                 voice2_id: int | None, scene_count: int, target_total_sec: int,
                 title: str = "", fast_preview: bool = True) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO stories (title, premise, character1_id, character2_id, voice1_id, voice2_id, "
            "scene_count, target_total_sec, fast_preview, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, premise, character1_id, character2_id, voice1_id, voice2_id,
             scene_count, target_total_sec, 1 if fast_preview else 0, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM stories WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_story(story_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone())


def list_stories() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM stories ORDER BY id DESC").fetchall())


def update_story(story_id: int, **fields) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in _STORY_UPDATABLE}
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with get_conn() as conn:
            conn.execute(f"UPDATE stories SET {cols} WHERE id = ?", (*updates.values(), story_id))
    return get_story(story_id)


def delete_story(story_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM story_scenes WHERE story_id = ?", (story_id,))
        conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))


# ---------- story_scenes ----------

def create_scene(story_id: int, position: int, background_id: int, situation: str = "",
                 line1: str = "", line2: str = "", transition: str = "fade") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO story_scenes (story_id, position, background_id, situation, line1, line2, "
            "transition, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, position, background_id, situation, line1, line2, transition, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM story_scenes WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_scene(scene_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM story_scenes WHERE id = ?", (scene_id,)).fetchone())


def list_scenes(story_id: int) -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM story_scenes WHERE story_id = ? ORDER BY position, id", (story_id,)
        ).fetchall())


def update_scene(scene_id: int, **fields) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in _SCENE_UPDATABLE}
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with get_conn() as conn:
            conn.execute(f"UPDATE story_scenes SET {cols} WHERE id = ?", (*updates.values(), scene_id))
    return get_scene(scene_id)


def delete_scene(scene_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM story_scenes WHERE id = ?", (scene_id,))


def repack_scene_positions(story_id: int) -> None:
    """After a delete/insert, renumber positions to a dense 0..N-1 in current order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM story_scenes WHERE story_id = ? ORDER BY position, id", (story_id,)
        ).fetchall()
        for new_pos, row in enumerate(rows):
            conn.execute("UPDATE story_scenes SET position = ? WHERE id = ?", (new_pos, row["id"]))


def reorder_scenes(story_id: int, ordered_scene_ids: list[int]) -> None:
    with get_conn() as conn:
        owned = {r["id"] for r in conn.execute(
            "SELECT id FROM story_scenes WHERE story_id = ?", (story_id,)
        ).fetchall()}
        for new_pos, sid in enumerate(sid for sid in ordered_scene_ids if sid in owned):
            conn.execute("UPDATE story_scenes SET position = ? WHERE id = ?", (new_pos, sid))


def invalidate_scene_render(scene_id: int) -> None:
    """Called when a scene's dialogue/background changes: drop its rendered clip and cached
    voice lines so the next render rebuilds them. Composite stays (identity-locked to the same
    photos; find_composite will still hit the cache for an unchanged background)."""
    with get_conn() as conn:
        conn.execute(
            # a scene already waiting in / running through the render queue keeps that status --
            # the worker re-reads the scene when its turn comes and renders the edited lines
            "UPDATE story_scenes SET clip_path = NULL, voice_line1_id = NULL, voice_line2_id = NULL, "
            "status = CASE WHEN status IN ('queued', 'rendering') THEN status ELSE 'pending' END, "
            "error_message = NULL WHERE id = ?",
            (scene_id,),
        )


# ---------- follow (유튜브 따라하기) ----------

_FOLLOW_UPDATABLE = {"title", "source_url", "setting", "background_id", "fast_preview", "published",
                     "ref_video_path", "length_sec", "use_still", "last_render_sec", "match_reference",
                     "description", "is_template", "template_id",
                     "final_video_path", "compare_video_path"}
_CAST_UPDATABLE = {"label", "character_id", "voice_id", "costume", "still_path"}
_CUT_UPDATABLE = {"render_seed", "cast_id", "cast_id2", "people", "look", "line", "action", "tone", "motion", "framing", "camera",
                  "ltx_prompt", "clip_path", "clip_sec", "timing", "spoken_line",
                  "render_sec", "status", "error_message", "ref_clip_path", "ref_thumb_path"}


def _update(table: str, allowed: set, row_id: int, fields: dict) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in allowed}
    with get_conn() as conn:
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*updates.values(), row_id))
        return row_to_dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def create_follow_project(title: str, source_url: str, ref_video_path: str, start: float,
                          length: float, setting: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO follow_projects (title, source_url, ref_video_path, source_start_sec, length_sec, "
            "setting, fast_preview, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (title, source_url, ref_video_path, start, length, setting, now_iso()),
        )
        return row_to_dict(conn.execute("SELECT * FROM follow_projects WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_follow_project(pid: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM follow_projects WHERE id = ?", (pid,)).fetchone())


def list_follow_projects(is_template: bool | None = None, published_only: bool = False) -> list[dict]:
    sql, args = "SELECT * FROM follow_projects WHERE 1=1", []
    if is_template is not None:
        sql += " AND is_template = ?"
        args.append(1 if is_template else 0)
    if published_only:
        sql += " AND published = 1"
    with get_conn() as conn:
        return rows_to_list(conn.execute(sql + " ORDER BY id DESC", args).fetchall())


def update_follow_project(pid: int, **fields) -> dict | None:
    return _update("follow_projects", _FOLLOW_UPDATABLE, pid, fields)


def delete_follow_project(pid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM follow_cuts WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM follow_cast WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM follow_projects WHERE id = ?", (pid,))


def create_follow_cast(pid: int, label: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO follow_cast (project_id, label, created_at) VALUES (?, ?, ?)",
                           (pid, label, now_iso()))
        return row_to_dict(conn.execute("SELECT * FROM follow_cast WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_follow_cast(cast_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM follow_cast WHERE id = ?", (cast_id,)).fetchone())


def list_follow_cast(pid: int) -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM follow_cast WHERE project_id = ? ORDER BY id", (pid,)).fetchall())


def update_follow_cast(cast_id: int, **fields) -> dict | None:
    return _update("follow_cast", _CAST_UPDATABLE, cast_id, fields)


def delete_follow_cast(cast_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE follow_cuts SET cast_id = NULL WHERE cast_id = ?", (cast_id,))
        conn.execute("DELETE FROM follow_cast WHERE id = ?", (cast_id,))


def create_follow_cut(pid: int, position: int, start: float, end: float, look: str = "right") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO follow_cuts (project_id, position, start_sec, end_sec, look, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (pid, position, start, end, look, now_iso()))
        conn.execute("UPDATE follow_cuts SET render_seed = (random() & 281474976710655) WHERE id = ?", (cur.lastrowid,))
        return row_to_dict(conn.execute("SELECT * FROM follow_cuts WHERE id = ?", (cur.lastrowid,)).fetchone())


def copy_follow_project(tid: int, frozen_prompts: dict[int, str], max_sec: float | None = None) -> int:
    """A viewer's run of a preset: same reference cuts, cast (stills reused) and the admin's prompts
    frozen per cut. Clips are the viewer's own, so the preset is never overwritten."""
    with get_conn() as conn:
        t = conn.execute("SELECT * FROM follow_projects WHERE id = ?", (tid,)).fetchone()
        cur = conn.execute(
            "INSERT INTO follow_projects (title, source_url, ref_video_path, source_start_sec, length_sec, setting, "
            "background_id, fast_preview, use_still, is_template, published, template_id, description, created_at, match_reference) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
            (t["title"], t["source_url"], t["ref_video_path"], t["source_start_sec"],
             min(t["length_sec"], max_sec) if max_sec else t["length_sec"], t["setting"],
             t["background_id"], t["fast_preview"], t["use_still"], tid, t["description"], now_iso(), t["match_reference"]))
        new_id = cur.lastrowid
        cast_map = {}
        used = None
        if max_sec:  # a 10s run should not list roles that only appear later in the video
            used = {r[key] for r in conn.execute(
                "SELECT cast_id, cast_id2 FROM follow_cuts WHERE project_id = ? AND start_sec < ?",
                (tid, max_sec - 0.05)).fetchall() for key in ("cast_id", "cast_id2")}
        for m in conn.execute("SELECT * FROM follow_cast WHERE project_id = ? ORDER BY id", (tid,)).fetchall():
            if used is not None and m["id"] not in used:
                continue
            c = conn.execute(
                "INSERT INTO follow_cast (project_id, label, character_id, voice_id, costume, still_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id, m["label"], m["character_id"], m["voice_id"], m["costume"], m["still_path"], now_iso()))
            cast_map[m["id"]] = c.lastrowid
        for k in conn.execute("SELECT * FROM follow_cuts WHERE project_id = ? ORDER BY position", (tid,)).fetchall():
            if max_sec and k["start_sec"] >= max_sec - 0.05:
                continue  # viewer chose a shorter length (10/20/30s)
            end = k["end_sec"]
            if max_sec and end > max_sec + 0.05:
                # the cut that straddles the chosen length ends there: a 20s run took the whole
                # 19.6-21.0s cut and came out 21.3s (2026-09-22)
                if max_sec - k["start_sec"] < 0.25:
                    continue
                end = max_sec
            conn.execute(
                "INSERT INTO follow_cuts (project_id, position, start_sec, end_sec, ref_clip_path, ref_thumb_path, "
                "cast_id, cast_id2, people, look, line, action, tone, motion, framing, camera, ltx_prompt, "
                "created_at, render_seed, spoken_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, k["position"], k["start_sec"], end, k["ref_clip_path"], k["ref_thumb_path"],
                 # Preserve the saved composition, including both cast members in a two-shot.
                 cast_map.get(k["cast_id"]), cast_map.get(k["cast_id2"]), k["people"], k["look"], k["line"],
                 k["action"], k["tone"], k["motion"], k["framing"], k["camera"],
                 frozen_prompts.get(k["id"], k["ltx_prompt"]), now_iso(), k["render_seed"], k["spoken_line"]))
        return new_id


def get_follow_cut(cut_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM follow_cuts WHERE id = ?", (cut_id,)).fetchone())


def list_follow_cuts(pid: int) -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM follow_cuts WHERE project_id = ? ORDER BY position, id", (pid,)).fetchall())


def update_follow_cut(cut_id: int, **fields) -> dict | None:
    return _update("follow_cuts", _CUT_UPDATABLE, cut_id, fields)


def list_voice_styles() -> list[dict]:
    with get_conn() as conn:
        return rows_to_list(conn.execute("SELECT * FROM voice_styles ORDER BY sort, key").fetchall())


def get_voice_style(key: str) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM voice_styles WHERE key = ?", (key,)).fetchone())


def put_voice_style(key: str, label: str, instruct: str, sample_text: str, face: str, respell: str,
                    sort: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO voice_styles (key, label, instruct, sample_text, face, respell, sort) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, label, instruct, sample_text, face, respell, sort))


def get_voice_tone(voice_id: int, tone: str) -> dict | None:
    """The reference clip of this voice speaking in that tone (made with Qwen3-TTS VoiceDesign)."""
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM voice_tones WHERE voice_id = ? AND tone = ?",
                                        (voice_id, tone)).fetchone())


def put_voice_tone(voice_id: int, tone: str, audio_path: str, reference_text: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO voice_tones (voice_id, tone, audio_path, reference_text) VALUES (?, ?, ?, ?)",
                     (voice_id, tone, audio_path, reference_text))


def voice_tone_keys(voice_id: int) -> list[str]:
    with get_conn() as conn:
        return [r["tone"] for r in conn.execute("SELECT tone FROM voice_tones WHERE voice_id = ?", (voice_id,))]


def get_follow_still(key: str) -> str | None:
    """A still made once for a person + outfit + place + shot size (see follow_render._shared_still)."""
    with get_conn() as conn:
        row = conn.execute("SELECT path FROM follow_stills WHERE key = ?", (key,)).fetchone()
        return row["path"] if row else None


def put_follow_still(key: str, path: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO follow_stills (key, path, created_at) VALUES (?, ?, ?)",
                     (key, path, now_iso()))


def get_translation(src: str, lang: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT dst FROM follow_translations WHERE src = ? AND lang = ?", (src, lang)).fetchone()
        return row["dst"] if row else None


def put_translation(src: str, lang: str, dst: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO follow_translations (src, lang, dst) VALUES (?, ?, ?)", (src, lang, dst))


def get_clip_cache(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT path, clip_sec FROM follow_clip_cache WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def put_clip_cache(key: str, path: str, clip_sec: float) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO follow_clip_cache (key, path, clip_sec, created_at) VALUES (?, ?, ?, ?)",
                     (key, path, clip_sec, now_iso()))


def delete_clip_cache(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM follow_clip_cache WHERE key = ?", (key,))


def list_motions(verified_only: bool = False) -> list[dict]:
    with get_conn() as conn:
        q = "SELECT * FROM motion_library" + (" WHERE verified = 1" if verified_only else "") + " ORDER BY sort, key"
        return rows_to_list(conn.execute(q).fetchall())


def put_motion(key: str, label: str, action_en: str, kind: str, closeup_ok: bool, sort: int) -> None:
    """Add or update a motion's text (keeps its test result unless the text changed)."""
    with get_conn() as conn:
        row = conn.execute("SELECT action_en FROM motion_library WHERE key = ?", (key,)).fetchone()
        if row and row["action_en"] == action_en:
            conn.execute("UPDATE motion_library SET label = ?, kind = ?, closeup_ok = ?, sort = ? WHERE key = ?",
                         (label, kind, int(closeup_ok), sort, key))
        else:
            conn.execute("INSERT OR REPLACE INTO motion_library (key, label, action_en, kind, closeup_ok, verified, "
                         "sort, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                         (key, label, action_en, kind, int(closeup_ok), sort, now_iso()))


def set_motion_result(key: str, verified: bool, motion_score: float | None, result: str, sample_path: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE motion_library SET verified = ?, motion_score = ?, result = ?, sample_path = ?, "
                     "updated_at = ? WHERE key = ?",
                     (int(verified), motion_score, result, sample_path, now_iso(), key))


def add_prompt_history(project_id: int, cut_id: int, position: int, template_id: int | None, prompt: str,
                       action: str, motion: str, framing: str, seed: int | None, clip_path: str | None,
                       from_cache: bool) -> None:
    with get_conn() as conn:
        last = conn.execute("SELECT prompt, seed, clip_path FROM prompt_history WHERE cut_id = ? ORDER BY id DESC LIMIT 1",
                            (cut_id,)).fetchone()
        if last and (last["prompt"], last["seed"], last["clip_path"]) == (prompt, seed, clip_path):
            return
        conn.execute("INSERT INTO prompt_history (project_id, cut_id, position, template_id, prompt, action, motion, "
                     "framing, seed, clip_path, from_cache, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (project_id, cut_id, position, template_id, prompt, action or "", motion or "", framing or "",
                      seed, clip_path, int(from_cache), now_iso()))


def list_prompt_history(template_id: int | None, position: int, limit: int = 30) -> list[dict]:
    """Prompts used for this cut of this preset, in any run (newest first, starred ones on top, no duplicates)."""
    with get_conn() as conn:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM prompt_history WHERE template_id IS ? AND position = ? ORDER BY starred DESC, id DESC LIMIT ?",
            (template_id, position, limit * 4)).fetchall())
    seen, out = set(), []
    for r in rows:
        if r["prompt"] in seen:
            continue
        seen.add(r["prompt"])
        out.append(r)
    return out[:limit]


def star_prompt_history(hid: int, starred: bool) -> dict | None:
    with get_conn() as conn:
        conn.execute("UPDATE prompt_history SET starred = ? WHERE id = ?", (int(starred), hid))
        return row_to_dict(conn.execute("SELECT * FROM prompt_history WHERE id = ?", (hid,)).fetchone())


def get_prompt_history(hid: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM prompt_history WHERE id = ?", (hid,)).fetchone())

