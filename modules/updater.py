import os
import re
import time
import subprocess
import requests
from config import VERSION, GITHUB_REPO, IS_FROZEN, INSTALL_EXE

EXPECTED_ASSET_NAME = "vizo.exe"


def _version_tuple(v: str) -> tuple:
    """Parse a version into a comparable tuple, tolerant of suffixes and
    differing segment counts. '1.2' -> (1,2,0); '1.3.0-beta' -> (1,3,0)."""
    parts = (v or "").lstrip("vV").split(".")
    nums = []
    for p in parts:
        m = re.match(r"\d+", p.strip())
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _pick_asset(assets: list) -> dict | None:
    """Pick the release asset to install.

    Layered preference: exact expected name → any non-debug .exe → any .exe.
    A bare 'first .exe wins' rule could install vi.log-debug.exe if a debug
    build is attached to the release before the main one.
    """
    exes = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    if not exes:
        return None
    for a in exes:
        if a["name"].lower() == EXPECTED_ASSET_NAME:
            return a
    for a in exes:
        if "debug" not in a["name"].lower():
            return a
    return exes[0]


def check_for_updates() -> dict:
    """Query GitHub for latest release. Returns {available, version, download_url, notes}."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            return {"available": False, "current_version": VERSION}
        r.raise_for_status()
        data = r.json()
        latest = data["tag_name"].lstrip("v")
        if _version_tuple(latest) > _version_tuple(VERSION):
            asset = _pick_asset(data.get("assets", []))
            if asset:
                return {
                    "available": True,
                    "version": latest,
                    "download_url": asset["browser_download_url"],
                    "notes": data.get("body", "")[:500],
                    "current_version": VERSION,
                }
        return {"available": False, "current_version": VERSION}
    except Exception as e:
        return {"available": False, "error": str(e), "current_version": VERSION}


def _restart_env() -> dict:
    """Environment for relaunching the updated exe.

    The Nuitka onefile bootstrap exports NUITKA_ONEFILE_* vars to its payload
    process. If the relaunched exe inherits them, its bootstrap thinks it is
    the worker child of the OLD (dying) process and runs the OLD payload from
    a temp dir that is being deleted — the post-update start crashes or
    silently runs the old version. PyInstaller has the same issue via _PYI_*.
    """
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("NUITKA_ONEFILE_")
        and k != "NUITKA_ORIGINAL_ARGV0"
        and not k.startswith("_PYI_")
    }


def download_and_apply(download_url: str) -> dict:
    """Download new exe, rename current → .old, put new in place, restart."""
    if not IS_FROZEN:
        return {"ok": False, "error": "Обновление работает только в собранном exe"}
    # INSTALL_EXE — путь к УСТАНОВЛЕННОМУ бинарнику. sys.executable в onefile
    # может указывать на распакованный во временную папку payload, замена
    # которого исчезает при выходе.
    exe_path = INSTALL_EXE
    old_path = exe_path + ".old"
    new_path = exe_path + ".new"

    try:
        # Clean leftovers
        for p in (old_path, new_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        # Download new version
        r = requests.get(download_url, stream=True, timeout=600)
        r.raise_for_status()
        expected = int(r.headers.get("Content-Length") or 0)
        written = 0
        with open(new_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)

        # Guard against a truncated download silently replacing the working exe.
        if written < 1_000_000 or (expected and written != expected):
            try:
                os.remove(new_path)
            except Exception:
                pass
            return {"ok": False, "error": f"Загрузка повреждена ({written} из {expected or '?'} байт)"}

        # Swap. Вторая rename может временно падать (антивирус сканирует
        # свежескачанный exe) — ретраим, а при окончательной неудаче
        # откатываем первую rename, иначе по пути установки не останется
        # ни одного exe и приложение больше не запустится.
        os.rename(exe_path, old_path)
        try:
            for attempt in range(5):
                try:
                    os.rename(new_path, exe_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.5)
        except Exception:
            try:
                os.rename(old_path, exe_path)  # restore original exe
            except Exception:
                pass
            raise

        # Restart. We're on a pywebview worker thread while the main thread is
        # blocked in the GUI loop, so sys.exit (SystemExit) would only kill THIS
        # thread, leaving the old process running alongside the new one.
        # os._exit terminates the whole process.
        subprocess.Popen(
            [exe_path],
            env=_restart_env(),
            cwd=os.path.dirname(exe_path) or None,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        os._exit(0)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cleanup_old_exe():
    """Remove .old leftover from previous update. Call on app startup."""
    if not IS_FROZEN:
        return
    old_path = INSTALL_EXE + ".old"
    if os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass
