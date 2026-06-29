"""Пресеты озвучки для voicer.mat3u.com.

Каждый пресет — полный набор параметров для POST /api/v1/voice/synthesize.
Поля совпадают с VoiceRequest из OpenAPI: voice_id, model_id, voice_settings,
split_type, max_chunk_length, split_output, auto_pause_*.
"""

VOICE_PRESETS = {
    # Тартария — русский голос (тот же voice_id что и у польского пресета)
    "tartaria": {
        "voice_id": "3EuKHIEZbSzrHGNmdYsx",
        "model_id": "eleven_v3",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.1,
        },
        "split_type": "smart",
        "max_chunk_length": 1000,
        "split_output": False,
        "auto_pause_enabled": True,
        "auto_pause_duration": 1.0,
        "auto_pause_frequency": 1,
    },
    "pl": {
        "voice_id": "3EuKHIEZbSzrHGNmdYsx",
        "model_id": "eleven_v3",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.1,
        },
        "split_type": "smart",
        "max_chunk_length": 1000,
        "split_output": False,
        "auto_pause_enabled": True,
        "auto_pause_duration": 1.0,
        "auto_pause_frequency": 1,
    },
    "hu": {
        "voice_id": "TumdjBNWanlT3ysvclWh",
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.0,
        },
        "split_type": "smart",
        "max_chunk_length": 2000,
        "split_output": False,
        "auto_pause_enabled": True,
        "auto_pause_duration": 0.5,
        "auto_pause_frequency": 1,
    },
    "cs": {
        "voice_id": "uju3wxzG5OhpWcoi3SMy",
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.5,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.0,
        },
        "split_type": "smart",
        "max_chunk_length": 2000,
        "split_output": False,
        "auto_pause_enabled": True,
        "auto_pause_duration": 0.5,
        "auto_pause_frequency": 1,
    },
}

# Локализованные имена языков из UI → ключ пресета
LANG_TO_PRESET = {
    "венгерский": "hu",
    "чешский": "cs",
    "польский": "pl",
}

TRANSLATE_LANGUAGES = list(LANG_TO_PRESET.keys())


def get_preset(key: str) -> dict:
    if key not in VOICE_PRESETS:
        available = ", ".join(VOICE_PRESETS.keys())
        raise KeyError(f"Неизвестный пресет озвучки: '{key}'. Доступные: {available}")
    return VOICE_PRESETS[key]


def resolve_lang(language: str) -> str:
    """Превращает локализованное имя языка (UI) в ключ пресета.

    Нормализуем регистр: из UI приходит «Венгерский», ключи — в нижнем."""
    key = LANG_TO_PRESET.get((language or "").strip().lower())
    if key is None:
        available = ", ".join(LANG_TO_PRESET.keys())
        raise KeyError(f"Язык '{language}' не поддерживается. Доступные: {available}")
    return key
