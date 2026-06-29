"""User-managed translation language list (for the «Перевод» screen).

Each entry: { id, name, flag, voice_id (ref to voices.py), prompt_template }
Stored in `user_settings/user_languages.json`.
"""
import json
import os
import uuid
from typing import Optional

from config import USER_DATA_DIR

LANGS_FILE = os.path.join(USER_DATA_DIR, "user_languages.json")

DEFAULT_LANGUAGES = [
    {"id": "hu", "name": "Венгерский", "flag": "🇭🇺", "voice_id": "hu",
     "prompt_template": ""},
    {"id": "cs", "name": "Чешский", "flag": "🇨🇿", "voice_id": "cs",
     "prompt_template": ""},
    {"id": "pl", "name": "Польский", "flag": "🇵🇱", "voice_id": "pl",
     "prompt_template": ""},
]


# «Файл есть, но не читается» ≠ «файла нет» — повреждённый файл нельзя
# молча затирать дефолтами, в нём пользовательские языки и промпт-шаблоны.
_CORRUPT = object()


def _read_file():
    if not os.path.exists(LANGS_FILE):
        return None
    try:
        with open(LANGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return _CORRUPT


def _write_file(langs: list) -> None:
    # Атомарная запись (как settings.py) — crash посреди записи не должен
    # оставить усечённый JSON.
    os.makedirs(os.path.dirname(LANGS_FILE) or ".", exist_ok=True)
    tmp = LANGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(langs, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LANGS_FILE)


def load_all() -> list:
    data = _read_file()
    if data is _CORRUPT:
        try:
            os.replace(LANGS_FILE, LANGS_FILE + ".bak")
        except Exception:
            pass
        data = None
    if data is None:
        data = list(DEFAULT_LANGUAGES)
        _write_file(data)
    return data


def get(lang_id: str) -> Optional[dict]:
    for l in load_all():
        if l.get("id") == lang_id:
            return l
    return None


def save(lang: dict) -> dict:
    langs = load_all()
    lid = lang.get("id")
    if not lid:
        lang["id"] = "l_" + uuid.uuid4().hex[:8]
        langs.append(lang)
    else:
        for i, l in enumerate(langs):
            if l.get("id") == lid:
                langs[i] = {**l, **lang}
                lang = langs[i]
                break
        else:
            langs.append(lang)
    _write_file(langs)
    return lang


def delete(lang_id: str) -> bool:
    langs = load_all()
    new_langs = [l for l in langs if l.get("id") != lang_id]
    if len(new_langs) == len(langs):
        return False
    _write_file(new_langs)
    return True


def reorder(ordered_ids: list) -> None:
    langs = load_all()
    by_id = {l.get("id"): l for l in langs}
    new_list = [by_id[i] for i in ordered_ids if i in by_id]
    # Keep any items not in the order list at the end
    for l in langs:
        if l.get("id") not in ordered_ids:
            new_list.append(l)
    _write_file(new_list)


def restore_defaults() -> int:
    langs = load_all()
    existing = {l.get("id") for l in langs}
    restored = 0
    for d in DEFAULT_LANGUAGES:
        if d["id"] not in existing:
            langs.append(dict(d))
            restored += 1
    if restored:
        _write_file(langs)
    return restored
