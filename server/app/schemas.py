from pydantic import BaseModel


class CompositeCreate(BaseModel):
    character1_id: int
    character2_id: int
    background_id: int


class DialogueCreate(BaseModel):
    keyword: str
    character1_id: int
    character2_id: int
    target_duration_sec: int = 20
    voice1_id: int | None = None


class DialogueUpdate(BaseModel):
    line1: str
    line2: str
    background_id: int | None = None


class DialogueManualCreate(BaseModel):
    character1_id: int
    character2_id: int
    line1: str
    line2: str = ""


class DialogueReplyRequest(BaseModel):
    line1: str
    background_id: int | None = None
    target_duration_sec: int = 20
    voice2_id: int | None = None


class VoiceLineCreate(BaseModel):
    text: str
    voice_id: int


class VideoCreate(BaseModel):
    composite_id: int
    dialogue_id: int
    voice1_id: int
    voice2_id: int
    ltx_prompt: str | None = None
    target_duration_sec: int = 20


class StoryCreate(BaseModel):
    premise: str
    character1_id: int
    character2_id: int
    voice1_id: int
    voice2_id: int
    scene_count: int = 6
    target_total_sec: int = 80
    background_ids: list[int]
    title: str = ""
    fast_preview: bool = True


class StoryUpdate(BaseModel):
    title: str | None = None
    premise: str | None = None
    target_total_sec: int | None = None
    fast_preview: bool | None = None
    music_gain: float | None = None
    intro_text: str | None = None
    outro_text: str | None = None


class SceneUpdate(BaseModel):
    situation: str | None = None
    line1: str | None = None
    line2: str | None = None
    caption: str | None = None
    transition: str | None = None
    background_id: int | None = None
    ltx_prompt: str | None = None


class SceneAdd(BaseModel):
    background_id: int
    position: int | None = None  # default: append at end


class SceneReorder(BaseModel):
    ordered_scene_ids: list[int]
