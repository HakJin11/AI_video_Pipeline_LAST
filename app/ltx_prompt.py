from app import ollama_client
from app.config import MANUAL_DIALOGUE_KEYWORD
from app.prompt_builder import build_ltx_prompt


def build_prompt(
    situation: str | None,
    char1: dict,
    char2: dict,
    bg: dict,
    line1: str,
    line2: str,
    t1: int,
    t2: int,
) -> str:
    """Ask gemma to author the full LTX prompt (dialogue + motion, with real character/background
    context from the DB); validate the exact dialogue lines survived; fall back to the
    deterministic template if gemma fails or drops/rewrites either line after retrying."""
    if situation and situation != MANUAL_DIALOGUE_KEYWORD:
        for _ in range(2):  # gemma occasionally drops/swaps a word despite the verbatim rule; retry once
            try:
                candidate = ollama_client.generate_video_prompt(
                    situation,
                    char1["name"],
                    char1["description"],
                    char2["name"],
                    char2["description"],
                    bg["description"],
                    line1,
                    line2,
                    t1,
                    t2,
                    char1["gender"],
                    char2["gender"],
                )
                if line1 in candidate and line2 in candidate:
                    return candidate
            except ollama_client.OllamaError:
                break  # fall back to the deterministic template below

    return build_ltx_prompt(
        char1["name"],
        char1["description"],
        char2["name"],
        char2["description"],
        bg["description"],
        line1,
        line2,
        t1,
        t2,
        situation,
        char1["gender"],
        char2["gender"],
    )
