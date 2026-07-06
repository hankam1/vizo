import os
import re
import sys
import time
import shutil
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
    """Pick the release asset for THIS platform.

    macOS: the zipped .app (vizo-macos.zip). Windows: the .exe (exact expected
    name → any non-debug .exe → any .exe; a bare 'first .exe wins' could install
    a debug build attached before the main one).
    """
    if sys.platform == "darwin":
        import platform
        machine = platform.machine().lower()  # 'arm64' (Apple Silicon) | 'x86_64' (Intel)
        is_arm = ("arm" in machine) or ("aarch64" in machine)
        dmgs = [a for a in assets if str(a.get("name", "")).lower().endswith(".dmg")]
        if dmgs:
            for a in dmgs:
                n = a["name"].lower()
                if is_arm and ("arm64" in n or "silicon" in n or "aarch64" in n):
                    return a
                if (not is_arm) and ("intel" in n or "x86" in n or "x64" in n):
                    return a
            return dmgs[0]  # arch not in name — best effort
        # Backward-compat: a zipped .app if no .dmg is attached.
        zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
        return zips[0] if zips else None
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


def _latest_tag_via_redirect() -> str | None:
    """Тег последнего релиза БЕЗ GitHub API.

    api.github.com без токена — 60 запросов/час НА IP-АДРЕС; за CGNAT
    (мобильный интернет) этот лимит общий на всех абонентов провайдера,
    поэтому у пользователей выскакивало «403 rate limit exceeded». Обычная
    веб-страница /releases/latest не лимитируется: тег читаем из редиректа."""
    r = requests.get(f"https://github.com/{GITHUB_REPO}/releases/latest",
                     timeout=10, allow_redirects=False)
    m = re.search(r"/releases/tag/([^/?#]+)", r.headers.get("Location") or "")
    return m.group(1) if m else None


def _asset_download_url(tag: str) -> str:
    """Прямая ссылка на ассет релиза — имена ассетов фиксированы CI-сборкой
    (vizo.exe, vizo-mac-apple-silicon.dmg, vizo-mac-intel.dmg)."""
    base = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}"
    if sys.platform == "darwin":
        import platform
        machine = platform.machine().lower()
        is_arm = ("arm" in machine) or ("aarch64" in machine)
        return f"{base}/" + ("vizo-mac-apple-silicon.dmg" if is_arm
                             else "vizo-mac-intel.dmg")
    return f"{base}/{EXPECTED_ASSET_NAME}"


def check_for_updates() -> dict:
    """Query GitHub for latest release. Returns {available, version, download_url, notes}.

    Сначала веб-редирект (без rate limit), API — только фолбэк и источник
    release notes: его отказ (403 и пр.) не должен ломать проверку."""
    try:
        tag = _latest_tag_via_redirect()
    except Exception:
        tag = None

    if tag:
        latest = tag.lstrip("vV")
        if _version_tuple(latest) <= _version_tuple(VERSION):
            return {"available": False, "current_version": VERSION}
        result = {
            "available": True,
            "version": latest,
            "download_url": _asset_download_url(tag),
            "notes": "",
            "current_version": VERSION,
        }
        # Release notes и точный URL ассета — бонус из API; без него тоже ок.
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10, headers={"Accept": "application/vnd.github+json"})
            if r.ok:
                data = r.json()
                result["notes"] = (data.get("body") or "")[:500]
                asset = _pick_asset(data.get("assets", []))
                if asset:
                    result["download_url"] = asset["browser_download_url"]
        except Exception:
            pass
        return result

    # Редирект не сработал (экзотический прокси и т.п.) — путь через API.
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            return {"available": False, "current_version": VERSION}
        if r.status_code == 403 and "rate limit" in (r.text or "").lower():
            return {"available": False, "current_version": VERSION,
                    "error": "GitHub временно ограничил проверки с этого "
                             "IP-адреса (общий лимит). Попробуй через час — "
                             "или скачай обновление вручную со страницы "
                             f"github.com/{GITHUB_REPO}/releases"}
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
    """Download the new build and apply it in place, then restart. Per-platform:
    Windows swaps the single .exe; macOS replaces the .app bundle."""
    if not IS_FROZEN:
        return {"ok": False, "error": "Обновление работает только в собранном приложении"}
    if sys.platform == "darwin":
        return _apply_macos(download_url)
    return _apply_windows(download_url)


def _apply_windows(download_url: str) -> dict:
    """Download new exe, rename current → .old, put new in place, restart."""
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


def _macos_app_path() -> str | None:
    """Path to the running .app bundle.

    Under a PyInstaller --windowed build, sys.executable is
    `…/vizo.app/Contents/MacOS/vizo`, so the bundle is three levels up."""
    p = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
    return p if p.endswith(".app") and os.path.isdir(p) else None


def _extract_app_from_dmg(dmg_path: str, workdir: str) -> str | None:
    """Mount the .dmg, copy the .app out, detach. Returns the copied .app path."""
    mp = os.path.join(workdir, "mnt")
    os.makedirs(mp, exist_ok=True)
    subprocess.run(["hdiutil", "attach", dmg_path, "-nobrowse", "-readonly",
                    "-mountpoint", mp], check=True)
    try:
        app = next((os.path.join(mp, n) for n in os.listdir(mp) if n.endswith(".app")), None)
        if not app:
            return None
        dest = os.path.join(workdir, os.path.basename(app))
        shutil.copytree(app, dest, symlinks=True)
        return dest
    finally:
        subprocess.run(["hdiutil", "detach", mp, "-force"], check=False)


def _apply_macos(download_url: str) -> dict:
    """Download the macOS build (.dmg, or legacy .zip), unpack the .app, strip
    the Gatekeeper quarantine (so an UNSIGNED build isn't blocked on relaunch),
    replace the running bundle in place, and relaunch with `open`."""
    import zipfile
    import tempfile

    app_path = _macos_app_path()
    if not app_path:
        return {"ok": False, "error": "Не удалось определить путь к vizo.app"}
    if not os.access(os.path.dirname(app_path), os.W_OK):
        return {"ok": False, "error": "Нет прав на запись рядом с vizo.app — перенеси приложение в «Программы» и попробуй снова"}

    tmpdir = tempfile.mkdtemp(prefix="vizo_update_")
    try:
        is_dmg = download_url.lower().split("?")[0].endswith(".dmg")
        dl_path = os.path.join(tmpdir, "update.dmg" if is_dmg else "update.zip")
        r = requests.get(download_url, stream=True, timeout=600)
        r.raise_for_status()
        expected = int(r.headers.get("Content-Length") or 0)
        written = 0
        with open(dl_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        if written < 1_000_000 or (expected and written != expected):
            return {"ok": False, "error": f"Загрузка повреждена ({written} из {expected or '?'} байт)"}

        if is_dmg:
            new_app = _extract_app_from_dmg(dl_path, tmpdir)
        else:
            extract_dir = os.path.join(tmpdir, "extracted")
            with zipfile.ZipFile(dl_path) as z:
                z.extractall(extract_dir)
            new_app = next((os.path.join(extract_dir, n) for n in os.listdir(extract_dir)
                            if n.endswith(".app")), None)
        if not new_app:
            return {"ok": False, "error": "Не нашёл .app в загруженном файле"}

        # Без подписи свежескачанный .app помечается карантином и Gatekeeper
        # блокирует запуск — снимаем атрибут, тогда блокировки нет.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", new_app], check=False)
        # zip может потерять бит исполняемости у бинарника — вернём его.
        macos_dir = os.path.join(new_app, "Contents", "MacOS")
        try:
            for fn in os.listdir(macos_dir):
                os.chmod(os.path.join(macos_dir, fn), 0o755)
        except Exception:
            pass

        # Подменяем бандл: старый в сторону, новый на место. На Unix можно
        # перемещать каталог работающего процесса — текущий exe продолжит жить.
        old_app = app_path + ".old"
        if os.path.exists(old_app):
            shutil.rmtree(old_app, ignore_errors=True)
        os.rename(app_path, old_app)
        try:
            shutil.move(new_app, app_path)
        except Exception:
            os.rename(old_app, app_path)  # откат
            raise

        subprocess.Popen(["open", app_path], env=_restart_env())
        os._exit(0)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "error": str(e)}


def cleanup_old_exe():
    """Remove the .old leftover from a previous update. Call on app startup.

    Сразу после обновления старый процесс ещё завершается и держит образ
    своего exe — одна мгновенная попытка удаления проигрывает эту гонку,
    и .old оставался лежать до следующего запуска. Лежащий рядом
    переименованный самообновлявшийся exe — красная тряпка для
    поведенческих эвристик антивирусов (Kaspersky PDM метил его как
    Trojan.Win32.Generic). Поэтому ретраим удаление в фоне ~60 секунд."""
    if not IS_FROZEN:
        return
    if sys.platform == "darwin":
        app_path = _macos_app_path()
        old = (app_path + ".old") if app_path else None
        if old and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        return
    old_path = INSTALL_EXE + ".old"
    if not os.path.exists(old_path):
        return

    def _retry_remove():
        for _ in range(60):
            try:
                os.remove(old_path)
                return
            except OSError:
                time.sleep(1.0)

    import threading
    threading.Thread(target=_retry_remove, daemon=True, name="old-exe-cleanup").start()
