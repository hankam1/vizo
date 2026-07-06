import asyncio
import os
import random
import re
import subprocess
import sys
import pyperclip
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import CHROME_PROFILE, PROMPTS_DIR
from modules.logger import get as get_logger
# ScenarioCancelled — обычный Exception: его ловят except Exception в раннере
# и api_bridge. asyncio.CancelledError (BaseException) пролетал мимо всех
# обработчиков и убивал поток без события «cancelled» в UI.
from modules.scenarios import ScenarioCancelled

log = get_logger("vizo.claude")


async def _human_pause(lo: float = 0.4, hi: float = 1.2):
    """Случайная пауза в диапазоне [lo; hi] секунд — имитирует человеческую
    задержку между действиями. Чтобы не выглядеть как бот, который щёлкает
    с миллисекундной точностью."""
    await asyncio.sleep(random.uniform(lo, hi))

CLAUDE_NEW_CHAT = "https://claude.ai/new"

# Модификатор для шорткатов редактирования: на macOS Chrome реагирует только
# на Cmd (Meta) — Ctrl+V там НЕ вставляет, из-за чего промпт уходил пустым
# (текст копировался в буфер, но в поле ввода ничего не появлялось).
EDIT_MODIFIER = "Meta" if sys.platform == "darwin" else "Control"


def _chrome_pid_alive(pid: int) -> bool:
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


def _ensure_profile_not_locked(profile_dir: str):
    """Убедиться, что профиль Chrome не занят другим ЖИВЫМ окном.

    Запускать второй экземпляр Chrome поверх занятого профиля — плохая идея:
    либо упадёт через несколько секунд, либо обе сессии полезут на claude.ai
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
                    "Закрой все окна Chrome, где открыт claude.ai с этим "
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
        if pid is not None and _chrome_pid_alive(pid):
            raise RuntimeError(
                f"Профиль Chrome уже используется другим окном (pid {pid}). "
                "Закрой все окна Chrome, где открыт claude.ai с этим "
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

def _is_browser_missing(err: BaseException) -> bool:
    """Похоже ли исключение Playwright на «браузер не установлен».

    Такие ошибки — повод попробовать следующий браузер из цепочки, а не
    падать. Любые другие ошибки запуска (занятый профиль, битые аргументы)
    пробрасываются как есть."""
    s = str(err)
    return ("playwright install" in s
            or "is not found at" in s
            or "Executable doesn't exist" in s)


def _download_bundled_chromium(say):
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


# Selectors (in order of preference)
INPUT_SELECTORS = [
    'div[contenteditable="true"][data-placeholder]',
    'div.ProseMirror',
    'div[contenteditable="true"]',
]
STOP_SELECTORS = [
    'button[aria-label="Stop"]',
    'button[aria-label="Stop generating"]',
    'button[data-testid="stop-button"]',
]
RESPONSE_SELECTORS = [
    '.font-claude-message',
    '[data-testid="assistant-message"]',
    '.prose',
]


class ClaudeAutomation:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def start(self, status_cb=None):
        def say(msg: str):
            log.info(msg)
            if status_cb:
                try:
                    status_cb(msg)
                except Exception:
                    pass

        self._pw = await async_playwright().start()

        os.makedirs(CHROME_PROFILE, exist_ok=True)
        _ensure_profile_not_locked(CHROME_PROFILE)

        launch_kwargs = dict(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
        )

        # Цепочка браузеров: системный Chrome → системный Edge (Chromium,
        # предустановлен почти на любой Windows) → Chromium самого Playwright
        # (докачивается при первом запуске). Раньше требовался строго Chrome —
        # у пользователей без него всё падало с «Run playwright install chrome».
        # Профиль один на всех: это одинаковый формат Chromium, а смена
        # флавора на конкретной машине — редкость (появился/пропал Chrome).
        for channel in ("chrome", "msedge", None):
            kwargs = dict(launch_kwargs)
            if channel:
                kwargs["channel"] = channel
            try:
                self._context = await self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as e:
                if not _is_browser_missing(e):
                    raise
                if channel is not None:
                    log.info("Браузер '%s' не найден — пробую следующий", channel)
                    continue
                # Встроенный Chromium ещё не скачан — качаем и пробуем снова.
                await asyncio.to_thread(_download_bundled_chromium, say)
                self._context = await self._pw.chromium.launch_persistent_context(**kwargs)
                break

        await asyncio.sleep(2)
        pages = self._context.pages
        self.page = pages[0] if pages else await self._context.new_page()

        # Закрыть лишние вкладки claude.ai в этом окне — несколько параллельных
        # сессий с одного аккаунта повышают подозрительность.
        try:
            for p in list(self._context.pages):
                if p is self.page or p.is_closed():
                    continue
                if "claude.ai" in (p.url or ""):
                    log.info("Закрываю лишнюю вкладку claude.ai: %s", p.url)
                    await p.close()
        except Exception as e:
            log.debug("Не смог закрыть лишние вкладки: %s", e)

        await self.page.goto(CLAUDE_NEW_CHAT, wait_until="domcontentloaded", timeout=30_000)
        # Баннер куки появляется только ПОСЛЕ перехода на claude.ai —
        # раньше мы пытались закрыть его ещё на about:blank.
        await self._dismiss_cookies()

        # If not logged in — the browser is visible (headless=False), so the
        # user signs in directly in that window. We must NOT call input() here:
        # the shipped GUI build has no console/stdin and input() raises
        # "lost sys.stdin", which broke the whole login flow. Instead poll the
        # URL until login completes.
        def _on_login_page() -> bool:
            url = self.page.url or ""
            return any(x in url for x in ("login", "signin", "auth"))

        # SPA может сделать client-side редирект на /login уже после
        # domcontentloaded — одна мгновенная проверка его пропускает и
        # link_claude рапортует успех без логина. Даём редиректу до 8с.
        needs_login = _on_login_page()
        if not needs_login:
            for _ in range(16):
                await asyncio.sleep(0.5)
                if _on_login_page():
                    needs_login = True
                    break
                # Поле ввода появилось — точно залогинены, дальше не ждём.
                if await self.page.locator(INPUT_SELECTORS[-1]).count() > 0:
                    break

        if needs_login:
            log.info("Claude: требуется вход — жду авторизации пользователя (до 5 мин)")
            deadline = asyncio.get_event_loop().time() + 300
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2)
                if not _on_login_page():
                    break
            else:
                raise RuntimeError(
                    "Не дождался входа в Claude. Войдите в аккаунт в открывшемся окне "
                    "браузера, затем запустите снова."
                )
            await self.page.goto(CLAUDE_NEW_CHAT, wait_until="domcontentloaded", timeout=30_000)

    async def close(self):
        # Каждый шаг закрытия независим: если context.close() упал (браузер
        # уже умер), playwright.stop() всё равно должен выполниться, иначе
        # остаётся висеть процесс драйвера, а исходная ошибка пайплайна
        # маскируется ошибкой close() из finally-блока вызывающего кода.
        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            log.warning("context.close() упал: %s", e)
        finally:
            self._context = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            log.warning("playwright.stop() упал: %s", e)
        finally:
            self._pw = None

    def is_alive(self) -> bool:
        """Check if the browser context and page are still usable."""
        try:
            return bool(self.page) and not self.page.is_closed() and bool(self._context)
        except Exception:
            return False

    async def new_chat(self):
        """Open a fresh chat in the existing browser (no restart).

        Avoids the persistent-context lock race condition that happens when you
        rapidly close+relaunch Chrome with the same user_data_dir — the second
        instance can die seconds after launch.

        Дополнительно: claude.ai иногда отвечает редиректом на
        `/api/challenge_redirect` (anti-bot), и если в этот момент кликать UI,
        элементы детачатся → клик падает по таймауту. Ждём пока url
        стабилизируется на /new и появится селектор модели.
        """
        if not self.is_alive():
            raise RuntimeError("Browser not alive — call start() first")
        await self.page.goto(CLAUDE_NEW_CHAT, wait_until="domcontentloaded", timeout=30_000)
        # 1) дождаться окончания challenge-редиректа: url должен прийти на /new
        for _ in range(20):  # до 20×0.5 = 10с
            url = self.page.url
            if "/new" in url and "challenge" not in url and "login" not in url:
                break
            await asyncio.sleep(0.5)
        # 2) дождаться когда селектор модели появится и стабилизируется
        try:
            await self.page.wait_for_selector(
                '[data-testid="model-selector-dropdown"]', timeout=15_000, state="visible"
            )
        except PWTimeout:
            log.warning("Селектор модели не появился после new_chat (url=%s)", self.page.url)
        # доп. settle — DOM иногда перерисовывается ещё пару раз
        await asyncio.sleep(1.5)

    # ------------------------------------------------------------------
    # Model selector
    # ------------------------------------------------------------------

    # Display names of supported models (matches dropdown text in claude.ai UI).
    # Состав меню на июль 2026 (проверено dom-dump'ом): Fable 5, Opus 4.8,
    # Sonnet 5, Haiku 4.5. Sonnet 4.6 из меню исчез — алиас в scenarios.py
    # мапит его на Sonnet 5 для старых сценариев. Fable 5 намеренно НЕ
    # поддерживаем: это промо «Included until July 7», пункт скоро пропадёт
    # из меню и сценарии с ним начали бы падать.
    SUPPORTED_MODELS = ["Opus 4.8", "Sonnet 5", "Haiku 4.5"]
    # Effort keys (data-testid suffixes) → отображаемый текст в подменю.
    # xhigh ("Extra") есть у Opus 4.8 и Sonnet 5. У Haiku effort
    # не настраивается вовсе (нет effort-menu-trigger).
    EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]

    async def set_model_and_effort(self, model_name: str | None,
                                    effort: str | None = None) -> bool:
        """Switch the current chat to the specified model AND effort in one
        round-trip through the dropdown. Both parameters are optional.

        model_name: "Opus 4.8" | "Sonnet 5" | "Haiku 4.5" | None (не менять)
        effort:     "low" | "medium" | "high" | "xhigh" | "max" | None
                    (xhigh = "Extra"; есть у Opus и Sonnet, у Haiku
                    effort не настраивается)

        Haiku не имеет настройки effort — параметр effort игнорируется.
        Возвращает True если хоть что-то применилось без ошибок.
        """
        if not model_name and not effort:
            return True

        level = effort.strip().lower() if effort else None
        if level and level not in self.EFFORT_LEVELS:
            log.warning("Неизвестный effort '%s' — пропускаю", effort)
            level = None
            effort = None

        # 0. Открыть меню моделей — это заставляет DOM подтвердить свежее
        # состояние (после new_chat() aria-label иногда отстаёт пока меню
        # не было открыто). Затем читаем актуальный aria-label кнопки.
        # На странице ровно один [data-testid="model-selector-dropdown"]
        # (проверено dom-dump'ом), его aria-label достоверный источник
        # вида "Model: Sonnet 5 Medium".
        if not await self._open_model_menu():
            return False
        await asyncio.sleep(0.3)  # дать DOM настояться после открытия
        current_label = await self._current_model_label()
        cur_model, cur_effort = self._parse_model_label(current_label or "")
        log.info("set_model_and_effort: цель=%s/%s, текущее=%s (model=%s, effort=%s)",
                 model_name, level, current_label, cur_model, cur_effort)

        need_model = bool(model_name) and (cur_model != model_name)
        # При смене модели claude.ai сбрасывает effort на дефолт новой
        # модели (Opus→High, Sonnet 5→Medium, Haiku→Extended). Если
        # меняем модель — effort выставляем заново, даже если формально
        # совпадает.
        need_effort = bool(level) and (need_model or cur_effort != level)
        if not need_model and not need_effort:
            log.info("Модель/effort уже '%s' — пропускаю переключение", current_label)
            await self._close_menus()
            return True

        # 2. Сменить модель (если нужно). Клик закрывает меню; верифицируем
        # через aria-label (источник правды, см. dom-dump). Ретраим до 3 раз
        # на случай если клик не дошёл до base-ui сразу после гидрации /new.
        model_ok = True
        if need_model:
            switched = False
            for attempt in range(3):
                # Меню должно быть открыто перед попыткой клика.
                if await self.page.locator('[role="menu"]').count() == 0:
                    if not await self._open_model_menu():
                        await asyncio.sleep(0.5)
                        continue
                try:
                    option = self.page.locator(
                        f'[role="menuitemradio"]:has-text("{model_name}")'
                    ).first
                    await option.click(timeout=5_000)
                    await _human_pause(0.6, 1.1)
                except Exception as e:
                    log.info("Клик по модели '%s' (попытка %d) упал: %s",
                             model_name, attempt + 1, e)
                cur_label_now = await self._current_model_label()
                cur_now, _ = self._parse_model_label(cur_label_now or "")
                if cur_now == model_name:
                    log.info("Модель переключена на '%s' (попытка %d)",
                             model_name, attempt + 1)
                    switched = True
                    break
                log.info("Модель не переключилась (aria-label='%s', нужно '%s') "
                         "— попытка %d/3",
                         cur_label_now, model_name, attempt + 1)
                await asyncio.sleep(0.5)
            if not switched:
                log.warning("Не смог переключить модель на '%s' после 3 попыток",
                            model_name)
                model_ok = False

        # 3. Effort. ВАЖНО (из dom-dump): `[data-testid="effort-menu-trigger"]`
        # на странице ВСЕГДА один и принадлежит только активной модели (для
        # Haiku его нет вообще). Поэтому скоупа по модели не нужно — нужно
        # лишь убедиться что активна нужная модель и меню открыто.
        if need_effort:
            applied = False
            for attempt in range(3):
                if await self.page.locator('[role="menu"]').count() == 0:
                    if not await self._open_model_menu():
                        await asyncio.sleep(0.5)
                        continue
                # Дать DOM настояться чтобы effort-menu-trigger успел появиться.
                trigger = self.page.locator('[data-testid="effort-menu-trigger"]').first
                try:
                    await trigger.wait_for(state="visible", timeout=2_500)
                except Exception:
                    pass
                if await trigger.count() == 0:
                    log.info("effort-menu-trigger отсутствует — видимо Haiku "
                             "(effort не настраивается)")
                    break
                try:
                    await trigger.click(timeout=4_000)
                    await _human_pause(0.5, 1.0)
                except Exception as e:
                    log.info("effort-trigger click (попытка %d) упал: %s",
                             attempt + 1, e)
                opt = self.page.locator(
                    f'[data-testid="effort-option-{level}"]'
                ).first
                if await opt.count() == 0:
                    log.warning("effort-option-%s не существует для текущей модели "
                                "(возможно, модель не поддерживает этот effort)", level)
                    break
                already_checked = False
                try:
                    already_checked = (
                        await opt.get_attribute("aria-checked", timeout=1_500)
                    ) == "true"
                except Exception:
                    pass
                if already_checked:
                    log.info("effort '%s' уже выбран — пропускаю клик", level)
                    applied = True
                    break
                try:
                    await opt.click(timeout=4_000)
                    await _human_pause(0.4, 0.9)
                except Exception as e:
                    log.info("effort-option-%s click (попытка %d) упал: %s",
                             level, attempt + 1, e)
                # Верифицируем через aria-label (правдив).
                cur_label_now = await self._current_model_label()
                cur_m_now, cur_eff_now = self._parse_model_label(cur_label_now or "")
                if cur_eff_now == level:
                    log.info("Effort переключен на '%s' (попытка %d)",
                             level, attempt + 1)
                    applied = True
                    break
                log.info("Effort не применился (aria-label='%s', нужно='%s') "
                         "— попытка %d/3",
                         cur_label_now, level, attempt + 1)
                await asyncio.sleep(0.5)
            if not applied and level:
                log.warning("Не смог переключить effort на '%s' после 3 попыток", level)

        # 4. Закрыть меню и прочитать финальный aria-label.
        await self._close_menus()
        final_label = await self._current_model_label()
        final_model, final_effort = self._parse_model_label(final_label or "")
        log.info("Final state: %s (model=%s, effort=%s)",
                 final_label, final_model, final_effort)

        model_ok = (not model_name) or (final_model == model_name)
        # У Haiku в aria-label стоит "Extended" (не один из наших EFFORT_LEVELS).
        # Это нормально — Haiku не настраивается, считаем что effort применён.
        effort_ok = (not level) or (final_effort == level) or (
            final_effort is None and "Haiku" in (final_model or "")
        )
        return model_ok and effort_ok

    async def _close_menus(self):
        """Закрыть все открытые popover-меню."""
        for _ in range(2):
            try:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            except Exception:
                pass

    async def set_model(self, model_name: str) -> bool:
        """Back-compat wrapper. Use set_model_and_effort for new code."""
        return await self.set_model_and_effort(model_name, None)

    async def _current_model_label(self) -> str | None:
        """Текущее значение aria-label кнопки селектора моделей.
        Пример: 'Model: Opus 4.8 High'. None если кнопка не найдена.
        На странице ровно один такой элемент (проверено dom-dump'ом)."""
        try:
            return await self.page.locator(
                '[data-testid="model-selector-dropdown"]'
            ).first.get_attribute("aria-label", timeout=3_000)
        except Exception:
            return None

    # Текстовая метка effort в aria-label → ключ в data-testid.
    # "Extended" — единственный режим Haiku, в нашем EFFORT_LEVELS его нет
    # (он не настраивается, поэтому return None и считаем «совпадает с любым»).
    _EFFORT_LABEL_TO_KEY = {
        "low": "low", "medium": "medium", "high": "high",
        "extra": "xhigh", "max": "max",
    }

    def _parse_model_label(self, label: str) -> tuple[str | None, str | None]:
        """'Model: Opus 4.8 High' → ('Opus 4.8', 'high')"""
        if not label or not label.lower().startswith("model:"):
            return None, None
        rest = label.split(":", 1)[1].strip()
        # Последнее слово label — это эффорт (Low/Medium/High/Extra/Max/Extended).
        parts = rest.rsplit(" ", 1)
        if len(parts) != 2:
            return rest, None
        model, effort_label = parts
        return model.strip(), self._EFFORT_LABEL_TO_KEY.get(effort_label.strip().lower())

    async def _open_model_menu(self) -> bool:
        """Открыть селектор моделей с ретраями. Возвращает True если меню
        реально появилось."""
        last_err = None
        for attempt in range(4):
            try:
                # Меню ещё закрыто — блуждающий base-ui оверлей мог остаться
                # от прошлого попапа и съесть клик по селектору.
                await self._neutralize_overlays()
                await self.page.locator(
                    '[data-testid="model-selector-dropdown"]'
                ).first.click(timeout=5_000)
                await _human_pause(0.5, 0.9)
                if await self.page.locator('[role="menu"]').count() > 0:
                    return True
            except Exception as e:
                last_err = e
                log.info("_open_model_menu: попытка %d не удалась (%s)",
                         attempt + 1, e)
                await asyncio.sleep(1.0)
        log.warning("Не открыл селектор моделей после 4 попыток: %s", last_err)
        return False

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _dismiss_cookies(self):
        try:
            btn = self.page.get_by_text("Accept All Cookies", exact=True)
            await btn.click(timeout=4_000)
            await asyncio.sleep(1)
        except Exception:
            pass

    async def _get_input(self):
        for sel in INPUT_SELECTORS:
            try:
                el = await self.page.wait_for_selector(sel, timeout=8_000)
                if el:
                    return el
            except PWTimeout:
                continue
        raise RuntimeError("Не найдено поле ввода Claude")

    async def _paste_text(self, text: str):
        """Copy text to clipboard and paste into the focused input."""
        pyperclip.copy(text)
        await self.page.keyboard.press(f"{EDIT_MODIFIER}+a")
        await _human_pause(0.15, 0.4)
        await self.page.keyboard.press(f"{EDIT_MODIFIER}+v")
        await _human_pause(0.5, 1.1)

    async def _upload_files(self, paths: list[str]):
        """Upload files via the hidden file input (works even if button not visible)."""
        attached = False
        try:
            file_input = await self.page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(paths)
                attached = True
        except Exception as e:
            log.warning("Загрузка через input[type=file] не удалась: %s", e)

        if not attached:
            # Fallback: look for attach / paperclip button
            attach_btn = await self.page.query_selector(
                'button[aria-label*="ttach"], button[data-testid*="attach"], '
                'button[aria-label*="ile"], label[for*="file"]'
            )
            if not attach_btn:
                # Молча отправить промпт БЕЗ примеров — тихая порча результата
                # (Claude сгенерирует не то без стиль-референсов). Падаем громко.
                raise RuntimeError(
                    "Не удалось прикрепить файлы к сообщению Claude: "
                    "не найден элемент загрузки. Возможно, claude.ai обновил вёрстку."
                )
            await self._neutralize_overlays()
            try:
                await attach_btn.click(timeout=5_000)
            except Exception as e:
                log.warning("Клик по кнопке вложений не прошёл (%s) — force", e)
                await attach_btn.click(timeout=5_000, force=True)
            await asyncio.sleep(0.5)
            file_input = await self.page.wait_for_selector('input[type="file"]', timeout=5_000)
            await file_input.set_input_files(paths)

        await self._wait_uploads_settle(paths)

    async def _wait_uploads_settle(self, paths: list[str]):
        """Дождаться окончания загрузки вложений перед отправкой.

        Фиксированных 2с не хватает для 7 PNG по ~0.5 МБ на медленной сети —
        сообщение уходило без части картинок. Ждём появления чипов вложений,
        а если селекторы не сработали — выдерживаем паузу, зависящую от
        суммарного размера файлов.
        """
        total_bytes = 0
        for p in paths:
            try:
                total_bytes += os.path.getsize(p)
            except OSError:
                pass
        # 2с базово + ~1с на МБ, максимум 20с.
        budget = min(2.0 + total_bytes / 1_000_000, 20.0)
        chip_selectors = (
            '[data-testid="file-thumbnail"], [data-testid*="attachment"], '
            'button[aria-label="Remove from chat"]'
        )
        waited = 0.0
        while waited < budget:
            try:
                n = await self.page.locator(chip_selectors).count()
                if n >= len(paths):
                    await asyncio.sleep(1.0)  # дать загрузке на сервер завершиться
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
            waited += 0.5

    async def send_message(self, text: str, file_paths: list[str] | None = None):
        """Type (via clipboard) and send a message. Optionally upload files first."""
        if not self.is_alive():
            raise RuntimeError(
                "Браузер Claude был закрыт. Если это случилось между шагами «Открыть новый чат» и отправкой промпта — возможно, профиль Chrome был занят другим окном. Попробуйте ещё раз."
            )
        if file_paths:
            await self._upload_files(file_paths)

        input_el = await self._get_input()
        # base-ui порталы claude.ai перекрывают страницу и перехватывают
        # клики (та же болезнь, что у кнопки copy в _last_response_text) —
        # без нейтрализации клик по полю ввода умирал по таймауту 30с.
        await self._neutralize_overlays()
        try:
            await input_el.click(timeout=5_000)
        except Exception as e:
            log.warning("Клик по полю ввода не прошёл (%s) — force/JS-фолбэк", e)
            await self._neutralize_overlays()
            try:
                await input_el.click(timeout=5_000, force=True)
            except Exception:
                # Последний рубеж: программный фокус. ProseMirror после
                # focus() принимает вставку с клавиатуры как после клика.
                await input_el.evaluate("el => el.focus()")
        await _human_pause(0.3, 0.7)
        await self._paste_text(text)

        # Перед отправкой — пауза «как будто читаем перед Send».
        # Длинные сообщения занимают чуть больше времени на «review».
        if len(text) > 5000:
            await _human_pause(1.5, 3.0)
        else:
            await _human_pause(0.6, 1.4)
        await self.page.keyboard.press("Enter")
        await _human_pause(1.2, 2.0)

    async def _safe_eval(self, script: str, default=None):
        """page.evaluate resilient to claude.ai's client-side navigations.

        Sending the first message in a fresh chat re-routes /new → /chat/<id>,
        which destroys the JS execution context; an evaluate caught mid-flight
        throws "Execution context was destroyed, most likely because of a
        navigation". The context is re-created within a moment, so we retry a
        couple of times and otherwise return `default` — a transient nav (or a
        closed page) must never crash the polling loop around us.
        """
        for attempt in range(3):
            try:
                return await self.page.evaluate(script)
            except Exception as e:
                msg = str(e).lower()
                if "destroyed" not in msg and "navigation" not in msg:
                    # Not a navigation blip (e.g. page closed) — give up quietly.
                    return default
                if attempt == 2:
                    return default
                await asyncio.sleep(0.3)
        return default

    async def _stop_button_present(self) -> bool:
        return await self._safe_eval("""
            () => {
                const selectors = [
                    'button[aria-label="Stop"]',
                    'button[aria-label="Stop generating"]',
                    'button[data-testid="stop-button"]',
                    'button[aria-label*="top response"]',
                ];
                for (const s of selectors) {
                    if (document.querySelector(s)) return true;
                }
                return false;
            }
        """, default=False)

    async def wait_for_response(self, timeout: int | None = None, min_growth: int = 30,
                                is_cancelled=None) -> str:
        """Wait for Claude to finish generating. No TOTAL time limit.

        Args:
            timeout: idle watchdog, in seconds (not a total cap). We never
                limit how long the whole generation may take — a long story
                can legitimately stream for many minutes — so this bounds only
                how long the page may make NO progress (visible text stops
                growing) before we treat claude.ai as stuck/frozen and bail
                out. timeout=None disables the watchdog entirely.
            min_growth: minimum text growth before considering length-based stability.
            is_cancelled: optional callable returning True when the user has
                cancelled the run. Polled inside both wait loops so long
                generations can be aborted.

        Strategy:
        - Phase 1 waits up to 60s for the Stop button to appear.
        - Phase 2 waits as long as needed for it to disappear.
        - Fallback: text length stability if Stop button never showed.
        """
        def check_cancel():
            if is_cancelled and is_cancelled():
                raise ScenarioCancelled("Сценарий отменён пользователем")

        # Idle watchdog instead of an absolute deadline. A long generation that
        # keeps streaming text must NOT be killed (a very long story can run
        # well past any fixed limit), but a page that froze — stuck generation
        # or a broken Stop-button selector — still has to bail out eventually
        # instead of hanging forever. So we abort only after the visible text
        # has made no progress for `timeout` seconds straight, and every chunk
        # of new text resets the clock.
        _loop = asyncio.get_event_loop()
        _idle_limit = timeout
        _last_progress = _loop.time()
        _last_seen_len = -1

        async def note_progress():
            nonlocal _last_progress, _last_seen_len
            cur = await self._safe_eval(
                "() => document.body.innerText.length", default=_last_seen_len)
            if cur != _last_seen_len:
                _last_seen_len = cur
                _last_progress = _loop.time()

        def check_deadline():
            if _idle_limit is not None and (_loop.time() - _last_progress) > _idle_limit:
                raise TimeoutError(
                    f"Claude не отвечает уже ~{_idle_limit}с — страница не меняется. "
                    "Возможно, claude.ai завис или изменил вёрстку — попробуйте ещё раз."
                )

        # Кнопка Copy появляется под ответом только когда он ДОГЕНЕРИРОВАН —
        # это прямой сигнал завершения. Запоминаем количество до ответа:
        # быстрые ответы успевают закончиться раньше, чем мы увидим Stop, и
        # раньше этот случай уходил в 60с ожидания + ложный таймаут.
        start_copies = await self._copy_button_count()

        async def finished_via_copy() -> bool:
            return (await self._copy_button_count()) > start_copies

        # Phase 1: wait up to 60s for stop button to appear (Claude started generating)
        started_deadline = asyncio.get_event_loop().time() + 60
        stop_seen = False
        while asyncio.get_event_loop().time() < started_deadline:
            check_cancel()
            if await self._stop_button_present():
                stop_seen = True
                break
            if await finished_via_copy():
                log.info("Claude finished generating (copy button, before stop seen)")
                await asyncio.sleep(1)
                return await self._last_response_text()
            await asyncio.sleep(0.5)

        if stop_seen:
            # Phase 2: wait for stop button to disappear — no time limit.
            gone_checks = 0
            while True:
                check_cancel()
                await note_progress()
                check_deadline()
                if not await self._stop_button_present():
                    gone_checks += 1
                    if gone_checks >= 4:  # 2s stable absence
                        await asyncio.sleep(1)  # small settle
                        log.info("Claude finished generating")
                        return await self._last_response_text()
                else:
                    gone_checks = 0
                await asyncio.sleep(0.5)

        # Fallback: stop-кнопку не увидели. Основной сигнал — появление copy,
        # запасной — полная неподвижность текста страницы достаточно долго,
        # чтобы пауза «думания» между абзацами (легко >10с) не сошла за конец.
        log.warning("Stop button never appeared — falling back to copy/length detection")
        stable = 0
        await asyncio.sleep(2)
        start_len = await self._safe_eval("() => document.body.innerText.length", default=0)
        prev_len = start_len
        while True:
            check_cancel()
            await note_progress()
            check_deadline()
            if await finished_via_copy():
                log.info("Claude finished generating (copy button, fallback)")
                await asyncio.sleep(1)
                return await self._last_response_text()
            # default=start_len → a transient nav reads as "no growth", so it
            # neither advances nor resets the stability counter.
            cur_len = await self._safe_eval(
                "() => document.body.innerText.length", default=start_len)
            if cur_len - start_len > min_growth:
                if cur_len == prev_len:
                    stable += 1
                    if stable >= 60:  # 30с полной неподвижности
                        log.info("Claude finished generating (length-based)")
                        return await self._last_response_text()
                else:
                    stable = 0
                    prev_len = cur_len
            await asyncio.sleep(0.5)

    async def _copy_button_count(self) -> int:
        return await self._safe_eval(
            "() => document.querySelectorAll('[data-testid=\"action-bar-copy\"]').length",
            default=0,
        )

    async def _last_response_text(self) -> str:
        # Scroll to bottom so virtualized content renders
        await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        # Новый UI claude.ai оставляет открытыми base-ui порталы
        # (`<div role="presentation" data-base-ui-inert>`), которые физически
        # перекрывают всю страницу и перехватывают клики. Escape не всегда
        # помогает (если portal anchored к другому popover'у), а force=True
        # обходит только actionability-check, не реальное перекрытие. Поэтому
        # отключаем pointer-events на оверлеях через JS — самый надёжный способ.
        await self._neutralize_overlays()

        # Strategy 1: copy-button click → clipboard (даёт чистый markdown)
        copy_buttons = await self.page.query_selector_all('[data-testid="action-bar-copy"]')
        log.info("_last_response_text: copy buttons found=%d", len(copy_buttons))
        if copy_buttons:
            sentinel = "__vilog_clipboard_sentinel__"
            target = copy_buttons[-1]
            pyperclip.copy(sentinel)
            await asyncio.sleep(0.1)
            clicked = False
            try:
                await target.click(timeout=4_000, force=True)
                clicked = True
            except Exception as e:
                log.warning("force-click по copy не прошёл: %s", e)
                try:
                    await target.evaluate("el => el.click()")
                    clicked = True
                except Exception as e2:
                    log.warning("JS-клик по copy тоже упал: %s", e2)
            if clicked:
                for _ in range(20):
                    await asyncio.sleep(0.2)
                    text = pyperclip.paste()
                    if text and text != sentinel:
                        log.info("_last_response_text: clipboard ok, len=%d", len(text))
                        return text.strip()
                log.warning("_last_response_text: clipboard sentinel не сменился за 4с")

        # Strategy 2: DOM scrape — расширенные селекторы под новый UI
        text = await self.page.evaluate("""
            () => {
                function clean(t) {
                    if (!t) return t;
                    const di = t.indexOf('Claude is AI');
                    if (di > 50) t = t.slice(0, di).trim();
                    return t;
                }
                // Свёрнутые «размышления» (extended thinking) рендерятся
                // ВНУТРИ контейнера ответа как pill со сводкой — innerText
                // захватывал её вместе с текстом, и «I notice the user
                // prompt is empty…» уезжал в script.txt и озвучку. Прячем
                // pill'ы на время чтения: display:none исключает их из
                // innerText. Кнопка copy этим не страдает — фикс только
                // для DOM-фолбэка.
                function extractClean(node) {
                    const pills = node.querySelectorAll('[class*="msg-pill"]');
                    const saved = [];
                    pills.forEach(p => { saved.push(p.style.display); p.style.display = 'none'; });
                    const txt = (node.innerText || '').trim();
                    pills.forEach((p, i) => { p.style.display = saved[i]; });
                    return txt;
                }
                const SELECTORS = [
                    '[data-testid="assistant-message"]',
                    '[data-test-render-count][data-message-role="assistant"]',
                    '[data-message-author-role="assistant"]',
                    '[data-message-role="assistant"]',
                    '.font-claude-message',
                    '.font-claude-response',
                    '.prose',
                ];
                for (const sel of SELECTORS) {
                    const nodes = document.querySelectorAll(sel);
                    if (nodes.length) {
                        const txt = extractClean(nodes[nodes.length - 1]);
                        if (txt) return clean(txt);
                    }
                }
                // Фолбэка «самый длинный div» здесь сознательно НЕТ: он
                // выбирал контейнер всего диалога (промпт + транскрипт +
                // ответ) и эта каша молча уходила в платную озвучку.
                // Честная ошибка дешевле тихо испорченного результата.
                return null;
            }
        """)
        if text:
            log.info("_last_response_text: DOM scrape ok, len=%d", len(text))
            return text.strip()

        raise RuntimeError("Не удалось извлечь текст ответа")

    async def _neutralize_overlays(self):
        """Disable invisible base-ui portals that intercept clicks.
        Safe to call repeatedly — only touches inert presentation overlays."""
        try:
            removed = await self.page.evaluate(
                """
                () => {
                    const sels = [
                        '[data-base-ui-inert]',
                        'div[role="presentation"][data-base-ui-portal]',
                    ];
                    let n = 0;
                    for (const sel of sels) {
                        document.querySelectorAll(sel).forEach(el => {
                            el.style.pointerEvents = 'none';
                            n++;
                        });
                    }
                    return n;
                }
                """
            )
            if removed:
                log.info("Нейтрализовано %d overlay-элементов", removed)
        except Exception as e:
            log.debug("neutralize_overlays failed: %s", e)

    # ------------------------------------------------------------------
    # High-level pipeline methods
    # ------------------------------------------------------------------

    async def run_tartaria(self, title: str, transcript: str) -> tuple[str, str]:
        """
        Full Tartaria pipeline (runs in one chat session):
          1. Send adaptation prompt → wait for confirmation → send 'Приступай'
          2. Wait for full ~35 000-char script
          3. In same chat: send pictures prompt + upload 7 example images
          4. Wait for ~100 image prompts
        Returns (script, image_prompts).
        """
        adaptation_path = os.path.join(PROMPTS_DIR, "tartaria", "adaptation.txt")
        pictures_path   = os.path.join(PROMPTS_DIR, "tartaria", "pictures.txt")
        examples_dir    = os.path.join(PROMPTS_DIR, "tartaria", "examples")

        with open(adaptation_path, encoding="utf-8") as f:
            adaptation_raw = f.read()
        with open(pictures_path, encoding="utf-8") as f:
            pictures_prompt = f.read()

        example_images = sorted(str(p) for p in Path(examples_dir).glob("*.png"))

        # Fill in the title in the first line
        adaptation_filled = adaptation_raw.replace(
            "Тема историй (оригинал):",
            f"Тема историй (оригинал): {title}",
            1,
        )
        full_message = f"{adaptation_filled}\n\n---\n\n{transcript}"

        # --- Step 1: send prompt, wait for Claude's confirmation ---
        print("\n[1/4] Отправляю промпт адаптации...")
        await self.send_message(full_message)

        print("[2/4] Жду подтверждения от Claude...")
        await self.wait_for_response(timeout=120)

        # --- Step 2: user types their reply ---
        print("\n" + "="*50)
        print("Claude ответил. Посмотри в браузере и введи своё сообщение:")
        print("="*50)
        user_reply = input("> ").strip()
        print("[3/4] Отправляю твой ответ...")
        await self.send_message(user_reply)

        print("      Жду готового сценария (до 20 мин)...")
        script = await self.wait_for_response(timeout=1200)

        # --- Step 3: image prompts in the SAME chat ---
        print("[4/4] Отправляю промпт для картинок + примеры...")
        await self.send_message(pictures_prompt, file_paths=example_images)

        print("      Жду 100 промптов (до 10 мин)...")
        image_prompts = await self.wait_for_response(timeout=600)

        return script, image_prompts

    async def _run_tartaria_with_ui(self, title: str, transcript: str, api,
                                    is_cancelled=None):
        """Tartaria flow that asks the user for reply via the UI instead of terminal."""
        adaptation_path = os.path.join(PROMPTS_DIR, "tartaria", "adaptation.txt")
        pictures_path   = os.path.join(PROMPTS_DIR, "tartaria", "pictures.txt")
        examples_dir    = os.path.join(PROMPTS_DIR, "tartaria", "examples")

        with open(adaptation_path, encoding="utf-8") as f:
            adaptation_raw = f.read()
        with open(pictures_path, encoding="utf-8") as f:
            pictures_prompt = f.read()

        example_images = sorted(str(p) for p in Path(examples_dir).glob("*.png"))

        adaptation_filled = adaptation_raw.replace(
            "Тема историй (оригинал):",
            f"Тема историй (оригинал): {title}",
            1,
        )
        full_message = f"{adaptation_filled}\n\n---\n\n{transcript}"

        api._progress("Отправляю промпт адаптации...", 18, 3)
        await self.send_message(full_message)
        claude_reply = await self.wait_for_response(timeout=300, is_cancelled=is_cancelled)

        api._progress("Жду твоего ответа...", 22, 4)
        user_reply = await api._wait_for_user_reply(claude_reply)

        api._progress("Генерирую сценарий (до 20 мин)...", 30, 5)
        await self.send_message(user_reply)
        self._last_script = await self.wait_for_response(timeout=1200, is_cancelled=is_cancelled)

        api._progress("Генерирую промпты для картинок...", 50, 6)
        await self.send_message(pictures_prompt, file_paths=example_images)
        self._last_image_prompts = await self.wait_for_response(timeout=600, is_cancelled=is_cancelled)

    async def run_translate(self, transcript: str, language: str,
                            is_cancelled=None) -> str:
        """
        Translate pipeline: fills [] with language, sends prompt + transcript.
        Returns translated text.
        """
        translate_path = os.path.join(PROMPTS_DIR, "translate", "translate.txt")

        with open(translate_path, encoding="utf-8") as f:
            translate_raw = f.read()

        prompt = translate_raw.replace("[]", language)
        full_message = f"{prompt}\n\n---\n\n{transcript}"

        print(f"\n[1/1] Отправляю промпт перевода на {language}...")
        await self.send_message(full_message)

        print("      Жду перевода (до 20 мин)...")
        result = await self.wait_for_response(timeout=1200, is_cancelled=is_cancelled)
        return result

    async def run_seo(self, title: str, description: str, language: str,
                      is_cancelled=None) -> tuple[str, str]:
        """Translate title and description in a new chat. Returns (seo_title, seo_description)."""
        title_path = os.path.join(PROMPTS_DIR, "translate", "seo_title.txt")
        desc_path  = os.path.join(PROMPTS_DIR, "translate", "seo_description.txt")

        with open(title_path, encoding="utf-8") as f:
            title_prompt = f.read().replace("[]", language).replace("{TITLE}", title)
        with open(desc_path, encoding="utf-8") as f:
            desc_prompt = f.read().replace("[]", language).replace("{DESCRIPTION}", description)

        print("\n[SEO] Открываю новый чат...")
        await self.new_chat()

        print("[SEO 1/2] Отправляю промпт для названия...")
        await self.send_message(title_prompt)
        seo_title = await self.wait_for_response(timeout=300, is_cancelled=is_cancelled)

        print("[SEO 2/2] Отправляю промпт для описания...")
        await self.send_message(desc_prompt)
        seo_description = await self.wait_for_response(timeout=600, is_cancelled=is_cancelled)

        return seo_title.strip(), seo_description.strip()
