"""Общие браузерные хелперы для провайдеров AI-чатов (Claude, ChatGPT).

Вынесены из claude_ui.py при добавлении второго провайдера (gpt_ui.py):
логика запуска persistent-контекста, разлочки профиля и докачки Chromium
одинакова для любого сайта — различается только профиль и целевой URL.
"""
import asyncio
import os
import random
import re
import subprocess
import sys

from modules.logger import get as get_logger

log = get_logger("vizo.browser")

# Модификатор для шорткатов редактирования: на macOS Chrome реагирует только
# на Cmd (Meta) — Ctrl+V там НЕ вставляет, из-за чего промпт уходил пустым
# (текст копировался в буфер, но в поле ввода ничего не появлялось).
EDIT_MODIFIER = "Meta" if sys.platform == "darwin" else "Control"


async def human_pause(lo: float = 0.4, hi: float = 1.2):
    """Случайная пауза в диапазоне [lo; hi] секунд — имитирует человеческую
    задержку между действиями. Чтобы не выглядеть как бот, который щёлкает
    с миллисекундной точностью."""
    await asyncio.sleep(random.uniform(lo, hi))


def chrome_pid_alive(pid: int) -> bool:
    """Жив ли процесс `pid` и похож ли он на Chrome.

    PID из протухшего лока после перезагрузки может достаться совсем другому
    процессу — тогда профиль на самом деле свободен. Если уточнить имя
    процесса не удалось, считаем занятым (консервативно)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError и пр. — процесс есть, но чужой
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                             capture_output=True, text=True, timeout=5)
        comm = (out.stdout or "").strip().lower()
        if comm and "chrom" not in comm:
            return False  # pid переиспользован не-Chrome процессом
    except Exception:
        pass
    return True


def ensure_profile_not_locked(profile_dir: str, site: str = "claude.ai"):
    """Убедиться, что профиль Chrome не занят другим ЖИВЫМ окном.

    Запускать второй экземпляр Chrome поверх занятого профиля — плохая идея:
    либо упадёт через несколько секунд, либо обе сессии полезут на сайт
    одновременно и это выглядит как два параллельных юзера.

    Протухшие lock-файлы (остаются после краша или жёсткого завершения
    Chrome) удаляются автоматически — ручное удаление не требуется.

    Windows: Chrome держит `lockfile` открытым (FILE_FLAG_DELETE_ON_CLOSE) —
    если файл удалился, он был протухшим; если нет — Chrome жив.

    macOS/Linux: `SingletonLock` — симлинк вида `hostname-pid`, обычно
    висячий, поэтому os.path.exists его НЕ видит — только lexists.
    Проверяем, что pid жив и это действительно процесс Chrome."""
    if os.name == "nt":
        lock = os.path.join(profile_dir, "lockfile")
        if os.path.exists(lock):
            try:
                os.remove(lock)
                log.info("Удалён протухший lockfile профиля Chrome")
            except OSError:
                raise RuntimeError(
                    "Профиль Chrome уже используется другим окном. "
                    f"Закрой все окна Chrome, где открыт {site} с этим "
                    "профилем, и попробуй ещё раз."
                )
        return

    lock = os.path.join(profile_dir, "SingletonLock")
    if os.path.lexists(lock):
        pid = None
        try:
            # target: "hostname-pid"; hostname не сверяем — на macOS он
            # меняется при смене сети, ложный «протухший» тут опаснее.
            pid_s = os.readlink(lock).rpartition("-")[2]
            if pid_s.isdigit():
                pid = int(pid_s)
        except OSError:
            pass
        if pid is not None and chrome_pid_alive(pid):
            raise RuntimeError(
                f"Профиль Chrome уже используется другим окном (pid {pid}). "
                f"Закрой все окна Chrome, где открыт {site} с этим "
                "профилем, и попробуй ещё раз."
            )
    # Лока нет или он протух (краш/чужой pid) — убираем все Singleton-файлы.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
                log.info("Удалён протухший %s профиля Chrome", name)
        except OSError as e:
            log.warning("Не смог удалить %s: %s", path, e)


def is_browser_missing(err: BaseException) -> bool:
    """Похоже ли исключение Playwright на «браузер не установлен».

    Такие ошибки — повод попробовать следующий браузер из цепочки, а не
    падать. Любые другие ошибки запуска (занятый профиль, битые аргументы)
    пробрасываются как есть."""
    s = str(err)
    return ("playwright install" in s
            or "is not found at" in s
            or "Executable doesn't exist" in s)


def download_bundled_chromium(say):
    """Скачать Chromium средствами Playwright-драйвера (~150 МБ, один раз).

    Вызывается, только если на машине нет ни Google Chrome, ни Microsoft
    Edge. Браузер кладётся в стандартный кэш Playwright (Windows:
    %LOCALAPPDATA%\\ms-playwright), поэтому переустановка или обновление
    vizo его не затирает и повторной закачки не будет.

    Блокирующая (subprocess.Popen + чтение stdout) — звать через
    asyncio.to_thread, иначе застынет event loop и с ним отмена/прогресс."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    say("Chrome и Edge не найдены — скачиваю Chromium (~150 МБ, один раз)…")
    popen_kwargs = {}
    if os.name == "nt":
        # GUI-сборка без консоли: без флага у пользователя мигнёт чёрное окно.
        popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    # --no-shell: не тащить headless-shell — мы запускаемся только headed,
    # а это минус ~половина объёма закачки.
    proc = subprocess.Popen(
        [str(node), str(cli), "install", "chromium", "--no-shell"],
        env=get_driver_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        **popen_kwargs)
    out = b""
    last_milestone = 0
    while True:
        # read1 — вернуть сколько есть, не ждать полного буфера: прогресс
        # драйвер рисует через \r без переводов строки, readline() бы завис.
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        out += chunk
        for m in re.finditer(rb"(\d{1,3})%", chunk):
            pct = min(int(m.group(1)), 100)
            milestone = pct - pct % 20
            if milestone > last_milestone and pct < 100:
                last_milestone = milestone
                say(f"Скачиваю Chromium… {pct}%")
    rc = proc.wait()
    if rc != 0:
        tail = out.decode("utf-8", "replace").strip().splitlines()[-5:]
        log.error("playwright install chromium failed (rc=%s): %s", rc, tail)
        raise RuntimeError(
            "На компьютере нет ни Google Chrome, ни Microsoft Edge, а "
            "скачать встроенный Chromium не получилось (проверь интернет "
            "и место на диске). Детали: " + " | ".join(tail))
    say("Chromium скачан")


async def launch_persistent_context(pw, profile_dir: str, say):
    """Запустить persistent-контекст на профиле `profile_dir`.

    Цепочка браузеров: системный Chrome → системный Edge (Chromium,
    предустановлен почти на любой Windows) → Chromium самого Playwright
    (докачивается при первом запуске). Раньше требовался строго Chrome —
    у пользователей без него всё падало с «Run playwright install chrome».
    Профиль один на всех: это одинаковый формат Chromium, а смена
    флавора на конкретной машине — редкость (появился/пропал Chrome)."""
    launch_kwargs = dict(
        user_data_dir=profile_dir,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            # Авто-отказ на запросы разрешений (уведомления и т.п.) —
            # клик по такому попапу вешал пайплайн, а разрешения
            # автоматизации всё равно не нужны.
            "--deny-permission-prompts",
        ],
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 900},
    )
    for channel in ("chrome", "msedge", None):
        kwargs = dict(launch_kwargs)
        if channel:
            kwargs["channel"] = channel
        try:
            return await pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            if not is_browser_missing(e):
                raise
            if channel is not None:
                log.info("Браузер '%s' не найден — пробую следующий", channel)
                continue
            # Встроенный Chromium ещё не скачан — качаем и пробуем снова.
            await asyncio.to_thread(download_bundled_chromium, say)
            return await pw.chromium.launch_persistent_context(**kwargs)
