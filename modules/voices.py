"""User-managed voice presets for voicer.mat3u.com.

Stored in `user_settings/voices.json`. Built-in defaults from
`modules.voice_templates` are seeded on first load (with builtin=True),
then user can edit/delete/duplicate freely. "Restore defaults" re-adds
deleted built-ins without touching custom voices.
"""
import copy
import json
import os
import uuid
from typing import Optional

from config import USER_DATA_DIR
from modules.voice_templates import VOICE_PRESETS

VOICES_FILE = os.path.join(USER_DATA_DIR, "user_voices.json")

DISPLAY_NAMES = {
    "tartaria": "Тартария",
    "pl": "Польский",
    "hu": "Венгерский",
    "cs": "Чешский",
}


def _builtin_seed() -> list:
    out = []
    for key, preset in VOICE_PRESETS.items():
        v = copy.deepcopy(preset)
        v["id"] = key
        v["name"] = DISPLAY_NAMES.get(key, key)
        v["builtin"] = True
        out.append(v)
    return out


# Маркер «файл есть, но прочитать не удалось» — это НЕ то же самое, что
# «файла нет»: затирать повреждённый файл сидом нельзя, в нём данные юзера.
_CORRUPT = object()


def _read_file():
    if not os.path.exists(VOICES_FILE):
        return None
    try:
        with open(VOICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return _CORRUPT


def _write_file(voices: list) -> None:
    # Атомарная запись (как settings.py): crash посреди json.dump не должен
    # оставить усечённый файл, который при следующем старте сотрёт все голоса.
    os.makedirs(os.path.dirname(VOICES_FILE) or ".", exist_ok=True)
    tmp = VOICES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, VOICES_FILE)


def load_all() -> list:
    data = _read_file()
    if data is _CORRUPT:
        # Откладываем повреждённый файл в .bak — данные можно будет вытащить.
        try:
            os.replace(VOICES_FILE, VOICES_FILE + ".bak")
        except Exception:
            pass
        data = None
    if data is None:
        data = _builtin_seed()
        _write_file(data)
    return data


def get(voice_id: str) -> Optional[dict]:
    for v in load_all():
        if v.get("id") == voice_id:
            return v
    return None


def save(voice: dict) -> dict:
    """Create or update a voice. Returns the saved voice."""
    voices = load_all()
    vid = voice.get("id")
    if not vid:
        voice["id"] = "v_" + uuid.uuid4().hex[:8]
        voice["builtin"] = False
        voices.append(voice)
    else:
        for i, v in enumerate(voices):
            if v.get("id") == vid:
                merged = {**v, **voice}
                voices[i] = merged
                voice = merged
                break
        else:
            voices.append(voice)
    _write_file(voices)
    return voice


def delete(voice_id: str) -> bool:
    voices = load_all()
    new_voices = [v for v in voices if v.get("id") != voice_id]
    if len(new_voices) == len(voices):
        return False
    _write_file(new_voices)
    return True


def duplicate(voice_id: str) -> Optional[dict]:
    src = get(voice_id)
    if not src:
        return None
    copy_v = copy.deepcopy(src)
    copy_v.pop("id", None)
    copy_v["builtin"] = False
    copy_v["name"] = (src.get("name") or src.get("id") or "Голос") + " (копия)"
    return save(copy_v)


def restore_defaults() -> int:
    """Re-add any built-in voices that were deleted. Does not overwrite
    existing entries (custom edits to built-ins are preserved). Returns
    count of restored voices."""
    voices = load_all()
    existing_ids = {v.get("id") for v in voices}
    restored = 0
    for builtin in _builtin_seed():
        if builtin["id"] not in existing_ids:
            voices.append(builtin)
            restored += 1
    if restored:
        _write_file(voices)
    return restored


def to_api_preset(voice: dict) -> dict:
    """Strip UI-only fields, return payload for voicer.mat3u.com."""
    api_keys = {
        "voice_id", "model_id", "voice_settings",
        "split_type", "max_chunk_length", "split_output",
        "auto_pause_enabled", "auto_pause_duration", "auto_pause_frequency",
    }
    return {k: voice[k] for k in api_keys if k in voice}
