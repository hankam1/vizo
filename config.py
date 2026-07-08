import os
import sys
from pathlib import Path

# Detect frozen build vs development mode.
# PyInstaller sets sys.frozen; Nuitka does NOT — it injects the module-level
# __compiled__ attribute instead. Without the Nuitka check, the onefile exe
# runs from a per-launch temp dir and writes ALL user data (API keys, Claude
# login profile, scenarios, logs) there — wiped on exit, so every relaunch
# starts blank. This was the root cause of "settings don't save in the .exe".
IS_FROZEN = bool(getattr(sys, "frozen", False) or ("__compiled__" in globals()))

if IS_FROZEN:
    # Bundled resources: PyInstaller extracts to sys._MEIPASS; Nuitka onefile
    # extracts next to __file__ (no _MEIPASS), so fall back to the module dir.
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    # Per-user writable data — MUST live outside the temp extraction dir so it
    # survives across launches. Per-OS conventional location.
    if sys.platform == "darwin":
        USER_DATA_DIR = os.path.join(str(Path.home()), "Library", "Application Support", "vizo")
    else:
        # Windows: keep the legacy "vi.log" folder so existing installs keep their data.
        APPDATA = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        USER_DATA_DIR = os.path.join(APPDATA, "vi.log")
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR
    USER_DATA_DIR = BASE_DIR

os.makedirs(USER_DATA_DIR, exist_ok=True)


def _install_exe_path() -> str:
    """Path to the INSTALLED executable (what the updater must replace).

    Under Nuitka onefile sys.executable can point to the payload extracted
    into %TEMP% (classic onefile mode), so replacing it would silently update
    a file that is wiped on exit. The onefile bootstrap exports the real
    binary's directory as NUITKA_ONEFILE_DIRECTORY, which Nuitka exposes as
    __compiled__.containing_dir; the original argv[0] keeps the binary name.
    PyInstaller's sys.executable already points at the installed exe.
    """
    if not IS_FROZEN:
        return sys.executable
    compiled = globals().get("__compiled__")
    if compiled is not None:  # Nuitka
        containing = getattr(compiled, "containing_dir", None)
        argv0 = os.environ.get("NUITKA_ORIGINAL_ARGV0") or (sys.argv[0] if sys.argv else "")
        name = os.path.basename(argv0) or os.path.basename(sys.executable)
        if containing and name:
            return os.path.join(containing, name)
        if argv0:
            return os.path.abspath(argv0)
    return sys.executable


INSTALL_EXE = _install_exe_path()

# Bundled resources (read-only)
PROMPTS_DIR = os.path.join(RESOURCE_DIR, "prompts")
UI_DIR = os.path.join(RESOURCE_DIR, "ui")

# Per-user data (writable)
OUTPUT_DIR = os.path.join(USER_DATA_DIR, "output") if IS_FROZEN else os.path.join(BASE_DIR, "output")
CHROME_PROFILE = os.path.join(USER_DATA_DIR, "chrome_profile")
# Отдельный профиль для ChatGPT: persistent-контекст жёстко лочит профиль
# одним инстансом Chrome, а в смешанном сценарии Claude и GPT открыты
# одновременно — значит, каждому провайдеру свой профиль (и своё окно).
GPT_CHROME_PROFILE = os.path.join(USER_DATA_DIR, "chrome_profile_gpt")
AUTH_STATE_FILE = os.path.join(USER_DATA_DIR, "claude_auth.json")

# Version (used by updater)
VERSION = "1.3.2"
GITHUB_REPO = "hankam1/vizo"

VOICE_API_BASE = "https://voicer.mat3u.com/api/v1"

# csv666 — вторая озвучка (https://voiceapi.csv666.ru/docs). Здесь голос
# задаётся UUID готового шаблона (template_uuid), а не настройками с нуля.
VOICE_CSV666_BASE = "https://voiceapi.csv666.ru"

# VeoNonStop — генерация картинок и видео (https://veononstop.org/api-docs.html)
VEO_BASE = "https://veononstop.org/api/v1"

VEO_IMAGE_MODELS = ["GEM_PIX_2", "NARWHAL"]
VEO_IMAGE_ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
VEO_VIDEO_ASPECT_RATIOS = ["16:9", "9:16"]
VEO_VIDEO_DURATIONS = ["4s", "6s", "8s"]
VEO_VIDEO_COUNTS = [1, 2, 3, 4]
VEO_VIDEO_MODES = ["text_to_video", "image_to_video", "multi_image", "batch_frame"]
