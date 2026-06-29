import json
import os
from config import USER_DATA_DIR, OUTPUT_DIR

SETTINGS_FILE = os.path.join(USER_DATA_DIR, "user_settings.json")

DEFAULT_OUTPUT_DIR = OUTPUT_DIR

DEFAULTS = {
    "veo_api_key": "",
    "voice_api_key": "",
    "voice_api_key_csv666": "",
    "output_dir": DEFAULT_OUTPUT_DIR,
    # Sound + popup when the queue finishes or a run fails (for overnight batches).
    "notify_on_complete": True,
    # How many times to auto-retry a run that failed with a TRANSIENT error
    # (network / timeout). 0 = off.
    "auto_retry": 1,
}


def load() -> dict:
    # Never let a corrupt/half-written settings file (which holds the API keys)
    # break startup — fall back to defaults instead of raising.
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save(data: dict):
    # Atomic write: dump to a temp file in the same dir, then os.replace so a
    # crash mid-write can't truncate/corrupt the existing settings.
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_FILE)
