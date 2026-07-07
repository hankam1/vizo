"""Автоматизация chatgpt.com через реальный браузер (Playwright).

Второй AI-провайдер рядом с Claude (claude_ui.py). Публичный интерфейс
зеркалит ClaudeAutomation, чтобы сценарный движок работал с обоими
одинаково: start / close / is_alive / new_chat / set_model /
send_message / wait_for_response.

Отличия от Claude:
- Свой Chrome-профиль (GPT_CHROME_PROFILE): persistent-контекст лочит
  профиль одним инстансом, а в смешанном сценарии оба чата открыты
  одновременно.
- Выбор модели «мягкий»: на бесплатном аккаунте у chatgpt.com пикера
  моделей может не быть вовсе — тогда пропускаем с предупреждением,
  а не роняем сценарий.
- effort-настройки нет — у ChatGPT это выбор модели (Instant/Thinking).
"""
import asyncio
import json
import os
import re
import pyperclip
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import GPT_CHROME_PROFILE
from modules.logger import get as get_logger
from modules.browser_common import (
    EDIT_MODIFIER,
    human_pause as _human_pause,
    ensure_profile_not_locked as _ensure_profile_not_locked,
    launch_persistent_context,
)
# ScenarioCancelled — обычный Exception: его ловят except Exception в раннере
# и api_bridge (см. комментарий в claude_ui.py).
from modules.scenarios import ScenarioCancelled

log = get_logger("vizo.gpt")

GPT_NEW_CHAT = "https://chatgpt.com/"

# Selectors (in order of preference). ChatGPT рендерит композер как
# ProseMirror contenteditable с id=prompt-textarea (проверено dom-дампом).
INPUT_SELECTORS = [
    '#prompt-textarea',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"]',
]
STOP_SELECTORS_JS = [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop streaming"]',
    'button[aria-label="Stop generating"]',
]
# Кнопка copy в action-баре под ГОТОВЫМ ответом — прямой сигнал завершения
# генерации (как action-bar-copy у Claude).
COPY_BUTTON_SELECTOR = '[data-testid="copy-turn-action-button"]'
# Чипы вложений в композере (файлы). Кандидаты — сверяются dom-дампом.
CHIP_SELECTORS = (
    '[data-testid*="attachment"], '
    'button[aria-label="Remove file"], button[aria-label*="Remove"]'
)


def _looks_like_login(url: str) -> bool:
    """Жёсткая страница логина (не модалка поверх чата)."""
    u = url or ""
    return ("auth.openai.com" in u
            or "auth0.openai.com" in u
            or "/auth/" in u
            or "/log-in" in u
            or "/login" in u)


class GPTAutomation:
    def __init__(self):
        self._pw = None
        self._context = None
        self.page = None
        # Базлайн copy-кнопок, снятый send_message ДО отправки. Быстрый ответ
        # (короткий промпт) успевает ДОГЕНЕРИРОВАТЬСЯ раньше, чем вызывающий
        # код дойдёт до wait_for_response — базлайн «после отправки» уже
        # включал бы готовый ответ, и ожидание висело бы до idle-таймаута.
        self._copies_before_send: int | None = None

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

        os.makedirs(GPT_CHROME_PROFILE, exist_ok=True)
        _ensure_profile_not_locked(GPT_CHROME_PROFILE, site="chatgpt.com")

        self._context = await launch_persistent_context(
            self._pw, GPT_CHROME_PROFILE, say)

        await asyncio.sleep(2)
        pages = self._context.pages
        self.page = pages[0] if pages else await self._context.new_page()

        # Закрыть лишние вкладки chatgpt.com — несколько параллельных сессий
        # с одного аккаунта повышают подозрительность (как у Claude).
        try:
            for p in list(self._context.pages):
                if p is self.page or p.is_closed():
                    continue
                if "chatgpt.com" in (p.url or ""):
                    log.info("Закрываю лишнюю вкладку chatgpt.com: %s", p.url)
                    await p.close()
        except Exception as e:
            log.debug("Не смог закрыть лишние вкладки: %s", e)

        await self.page.goto(GPT_NEW_CHAT, wait_until="domcontentloaded",
                             timeout=45_000)
        await self._dismiss_cookies()

        # chatgpt.com работает и БЕЗ логина (анонимный чат), поэтому логин
        # не обязателен: блокирует только жёсткий редирект на страницу
        # авторизации (в некоторых регионах/сессиях). Модалку «Log in /
        # Stay logged out» поверх чата закрываем — пайплайн без человека
        # должен ехать дальше в анонимном режиме.
        needs_login = _looks_like_login(self.page.url)
        if not needs_login:
            for _ in range(16):
                await asyncio.sleep(0.5)
                if _looks_like_login(self.page.url):
                    needs_login = True
                    break
                if await self.page.locator(INPUT_SELECTORS[0]).count() > 0:
                    break

        if needs_login:
            say("ChatGPT: требуется вход — жду авторизации (до 5 мин)")
            deadline = asyncio.get_event_loop().time() + 300
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2)
                if not _looks_like_login(self.page.url):
                    break
            else:
                raise RuntimeError(
                    "Не дождался входа в ChatGPT. Войдите в аккаунт в "
                    "открывшемся окне браузера, затем запустите снова."
                )
            await self.page.goto(GPT_NEW_CHAT, wait_until="domcontentloaded",
                                 timeout=45_000)

        await self._dismiss_login_modal()

    async def close(self):
        # Каждый шаг закрытия независим (см. claude_ui.close).
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
        """Открыть свежий чат в живом браузере (без перезапуска) — тот же
        приём, что у Claude: перезапуск persistent-контекста подряд ловит
        гонку на локе профиля."""
        if not self.is_alive():
            raise RuntimeError("Browser not alive — call start() first")
        await self.page.goto(GPT_NEW_CHAT, wait_until="domcontentloaded",
                             timeout=45_000)
        # Дождаться, пока SPA догидрируется и появится композер.
        try:
            await self.page.wait_for_selector(
                ", ".join(INPUT_SELECTORS), timeout=15_000, state="visible")
        except PWTimeout:
            log.warning("Композер не появился после new_chat (url=%s)",
                        self.page.url)
        await self._dismiss_login_modal()
        await asyncio.sleep(1.5)

    # ------------------------------------------------------------------
    # Model selector
    # ------------------------------------------------------------------

    # Названия пунктов меню моделей chatgpt.com (сверяется dom-дампом при
    # обновлениях). На бесплатном аккаунте пикер может отсутствовать —
    # тогда выбор модели мягко пропускается.
    SUPPORTED_MODELS = ["Auto", "Instant", "Thinking"]

    MODEL_MENU_TRIGGERS = [
        '[data-testid="model-switcher-dropdown-button"]',
        'button[aria-label*="Model selector"]',
        'button[aria-haspopup="menu"][aria-label*="model" i]',
    ]

    async def set_model(self, model_name: str | None) -> bool:
        """Переключить модель текущего чата. МЯГКАЯ операция: на бесплатном
        аккаунте пикера моделей нет — возвращаем True с предупреждением в
        лог, чтобы сценарий продолжался на дефолтной модели.

        Возвращает False только если пикер ЕСТЬ, но переключить на
        `model_name` не удалось."""
        if not model_name:
            return True
        model_name = model_name.strip()
        if not model_name:
            return True

        trigger = None
        for sel in self.MODEL_MENU_TRIGGERS:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    trigger = loc
                    break
            except Exception:
                continue
        if trigger is None:
            log.warning("Пикер моделей ChatGPT не найден (бесплатный аккаунт "
                        "без выбора модели?) — оставляю модель по умолчанию, "
                        "запрошено '%s'", model_name)
            return True

        for attempt in range(3):
            try:
                await trigger.click(timeout=5_000)
                await _human_pause(0.5, 0.9)
            except Exception as e:
                log.info("Клик по пикеру моделей (попытка %d) упал: %s",
                         attempt + 1, e)
                await asyncio.sleep(0.7)
                continue
            if await self.page.locator('[role="menu"]').count() == 0:
                continue
            try:
                option = self.page.locator(
                    f'[role="menuitem"]:has-text("{model_name}"), '
                    f'[role="menuitemradio"]:has-text("{model_name}")'
                ).first
                if await option.count() == 0:
                    log.warning("Пункта '%s' нет в меню моделей ChatGPT",
                                model_name)
                    await self._close_menus()
                    return False
                await option.click(timeout=5_000)
                await _human_pause(0.6, 1.1)
            except Exception as e:
                log.info("Клик по модели '%s' (попытка %d) упал: %s",
                         model_name, attempt + 1, e)
                await self._close_menus()
                continue
            # Верификация: текст кнопки-триггера обычно содержит выбранную
            # модель. Если прочитать не удалось — верим клику.
            try:
                label = (await trigger.inner_text(timeout=2_000)) or ""
            except Exception:
                label = ""
            if not label or model_name.lower() in label.lower():
                log.info("Модель ChatGPT переключена на '%s' (попытка %d)",
                         model_name, attempt + 1)
                await self._close_menus()
                return True
            log.info("Модель не переключилась (кнопка='%s', нужно '%s') — "
                     "попытка %d/3", label.strip(), model_name, attempt + 1)
        log.warning("Не смог переключить модель ChatGPT на '%s'", model_name)
        await self._close_menus()
        return False

    async def _close_menus(self):
        """Закрыть все открытые popover-меню."""
        for _ in range(2):
            try:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _dismiss_cookies(self):
        for label in ("Accept all", "Accept All Cookies", "Принять все"):
            try:
                btn = self.page.get_by_text(label, exact=True).first
                await btn.click(timeout=3_000)
                await asyncio.sleep(0.8)
                return
            except Exception:
                continue

    async def _dismiss_login_modal(self):
        """Закрыть модалку «Log in or sign up» / «Welcome back», которую
        chatgpt.com показывает анонимам поверх чата. Без человека за
        компьютером пайплайн должен ехать дальше в анонимном режиме.
        Best-effort: ищем ссылку «Stay logged out» внутри диалога, затем
        крестик закрытия."""
        try:
            stay = self.page.get_by_text("Stay logged out", exact=False).first
            if await stay.count() > 0 and await stay.is_visible():
                await stay.click(timeout=3_000)
                log.info("Закрыл модалку логина через «Stay logged out»")
                await asyncio.sleep(0.6)
                return
        except Exception:
            pass
        try:
            n = await self.page.evaluate(
                """
                () => {
                    let n = 0;
                    for (const d of document.querySelectorAll('div[role="dialog"]')) {
                        const b = d.querySelector(
                            'button[aria-label="Close"], button[data-testid="close-button"]');
                        if (b) { b.click(); n++; }
                    }
                    return n;
                }
                """
            )
            if n:
                log.info("Закрыто %d диалогов поверх чата", n)
                await asyncio.sleep(0.5)
        except Exception as e:
            log.debug("dismiss_login_modal failed: %s", e)

    async def _get_input(self):
        for sel in INPUT_SELECTORS:
            try:
                el = await self.page.wait_for_selector(sel, timeout=8_000)
                if el:
                    return el
            except PWTimeout:
                continue
        raise RuntimeError("Не найдено поле ввода ChatGPT")

    async def _input_is_focused(self, input_el) -> bool:
        """True, если фокус сейчас внутри contenteditable-редактора.
        Устойчива к протухшему handle (см. claude_ui)."""
        try:
            if input_el is not None and await input_el.evaluate("el => el.isConnected"):
                return bool(await input_el.evaluate(
                    "el => el === document.activeElement"
                    " || el.contains(document.activeElement)"))
        except Exception:
            pass
        return bool(await self._safe_eval(
            """() => {
                const ae = document.activeElement;
                return !!(ae && ae.closest
                          && ae.closest('div[contenteditable="true"]'));
            }""", default=False))

    async def _editor_contains(self, text: str) -> bool:
        """Есть ли начало `text` в ЖИВОМ редакторе (по DOM страницы, а не по
        возможно-протухшему handle). Сравниваем без пробелов: ProseMirror
        режет текст на параграфы."""
        probe = re.sub(r"\s+", "", text)[:60]
        if not probe:
            return True
        script = """
            () => {
                const probe = %s;
                const eds = document.querySelectorAll(
                    '#prompt-textarea, div.ProseMirror, div[contenteditable="true"]');
                for (const el of eds) {
                    if ((el.textContent || '').replace(/\\s+/g, '').includes(probe))
                        return true;
                }
                return false;
            }
        """ % json.dumps(probe)
        return bool(await self._safe_eval(script, default=False))

    async def _composer_chip_count(self) -> int:
        """Число чипов-вложений в композере."""
        n = await self._safe_eval(
            """() => document.querySelectorAll(%s).length"""
            % json.dumps(CHIP_SELECTORS), default=0)
        return int(n or 0)

    async def _paste_text(self, text: str, input_el=None, chip_baseline: int = 0):
        """Copy text to clipboard and paste into the focused input.
        Верификация по живому DOM (см. подробный разбор в claude_ui):
        протухший handle ProseMirror «не видит» текст, а очень длинную
        вставку композер может конвертировать в чип-вложение."""
        for attempt in range(2):
            pyperclip.copy(text)
            await self.page.keyboard.press(f"{EDIT_MODIFIER}+a")
            await _human_pause(0.15, 0.4)
            await self.page.keyboard.press(f"{EDIT_MODIFIER}+v")
            await _human_pause(0.5, 1.1)
            if input_el is None:
                return
            for _ in range(6):
                if await self._editor_contains(text):
                    return
                if await self._composer_chip_count() > chip_baseline:
                    log.info("Вставка ушла чипом-вложением — это успех")
                    return
                await asyncio.sleep(0.4)
            log.warning("Вставка не дала текста в редакторе (попытка %d/2)",
                        attempt + 1)
            try:
                input_el = await self._get_input()
                await input_el.click(timeout=5_000)
            except Exception as e:
                log.warning("Не смог перефокусировать редактор для повтора: %s", e)
            if not await self._input_is_focused(input_el):
                break
        raise RuntimeError(
            "Текст промпта не вставился в поле ввода ChatGPT — вставка из "
            "буфера обмена не сработала. Попробуй запустить шаг ещё раз.")

    async def _upload_files(self, paths: list[str]):
        """Upload files via the hidden file input (works even if button not
        visible). ВАЖНО: анонимный chatgpt.com не даёт прикреплять файлы —
        для шагов с вложениями нужен вход в аккаунт."""
        attached = False
        try:
            file_input = await self.page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(paths)
                attached = True
        except Exception as e:
            log.warning("Загрузка через input[type=file] не удалась: %s", e)

        if not attached:
            attach_btn = await self.page.query_selector(
                '[data-testid="composer-plus-btn"], '
                'button[aria-label*="Add photos"], button[aria-label*="Attach"], '
                'button[aria-label*="Add files"]'
            )
            if not attach_btn:
                # Молча отправить промпт БЕЗ примеров — тихая порча
                # результата. Падаем громко (как у Claude).
                raise RuntimeError(
                    "Не удалось прикрепить файлы к сообщению ChatGPT: не "
                    "найден элемент загрузки. Если ты не вошёл в аккаунт — "
                    "анонимный ChatGPT не принимает вложения; войди через "
                    "Настройки → ChatGPT. Либо chatgpt.com обновил вёрстку."
                )
            try:
                await attach_btn.click(timeout=5_000)
            except Exception as e:
                log.warning("Клик по кнопке вложений не прошёл (%s) — force", e)
                await attach_btn.click(timeout=5_000, force=True)
            await asyncio.sleep(0.5)
            file_input = await self.page.wait_for_selector(
                'input[type="file"]', timeout=5_000)
            await file_input.set_input_files(paths)

        await self._wait_uploads_settle(paths)

    async def _wait_uploads_settle(self, paths: list[str]):
        """Дождаться окончания загрузки вложений перед отправкой
        (бюджет по суммарному размеру, как у Claude)."""
        total_bytes = 0
        for p in paths:
            try:
                total_bytes += os.path.getsize(p)
            except OSError:
                pass
        budget = min(2.0 + total_bytes / 1_000_000, 20.0)
        waited = 0.0
        while waited < budget:
            try:
                n = await self.page.locator(CHIP_SELECTORS).count()
                if n >= len(paths):
                    await asyncio.sleep(1.0)  # дать загрузке завершиться
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
            waited += 0.5

    async def send_message(self, text: str, file_paths: list[str] | None = None):
        """Type (via clipboard) and send a message. Optionally upload files first."""
        if not self.is_alive():
            raise RuntimeError(
                "Браузер ChatGPT был закрыт. Если это случилось между шагами "
                "«Открыть новый чат» и отправкой промпта — возможно, профиль "
                "Chrome был занят другим окном. Попробуйте ещё раз."
            )
        await self._dismiss_login_modal()
        if file_paths:
            await self._upload_files(file_paths)

        input_el = await self._get_input()
        # Клавиатуру шлём ТОЛЬКО убедившись, что фокус реально в редакторе
        # (иначе Ctrl+A выделяет страницу — см. историю бага в claude_ui).
        focused = False
        for attempt in range(4):
            if attempt:
                try:
                    input_el = await self._get_input()
                except Exception:
                    pass
            await self._dismiss_login_modal()
            try:
                await input_el.click(timeout=5_000)
            except Exception as e:
                log.warning("Клик по полю ввода не прошёл (%s) — force/JS-фолбэк", e)
                try:
                    await input_el.click(timeout=5_000, force=True)
                except Exception:
                    try:
                        await input_el.evaluate("el => el.focus()")
                    except Exception:
                        pass
            await _human_pause(0.3, 0.7)
            focused = await self._input_is_focused(input_el)
            if focused:
                break
            log.info("Фокус не в поле ввода (попытка %d/4) — Escape и повтор",
                     attempt + 1)
            await self._close_menus()
        if not focused:
            raise RuntimeError(
                "Не удалось сфокусировать поле ввода ChatGPT: клики "
                "перехватывает попап или баннер. Закрой лишние попапы в окне "
                "браузера и запусти шаг ещё раз."
            )
        chips_before = await self._composer_chip_count()
        await self._paste_text(text, input_el, chips_before)

        # Пауза «как будто читаем перед Send».
        if len(text) > 5000:
            await _human_pause(1.5, 3.0)
        else:
            await _human_pause(0.6, 1.4)
        if await self.page.locator('[role="menu"]').count() > 0:
            log.info("Перед отправкой открыто постороннее меню — закрываю")
            await self._close_menus()
        if not await self._input_is_focused(input_el):
            try:
                await input_el.click(timeout=5_000)
            except Exception:
                input_el = await self._get_input()
                await input_el.click(timeout=5_000)
            if not await self._input_is_focused(input_el):
                raise RuntimeError(
                    "Фокус ушёл из поля ввода ChatGPT перед отправкой — "
                    "сообщение не отправлено. Запусти шаг ещё раз.")
        # Базлайн ДО отправки — см. комментарий у _copies_before_send.
        self._copies_before_send = await self._copy_button_count()
        await self.page.keyboard.press("Enter")
        await _human_pause(1.2, 2.0)

    async def _safe_eval(self, script: str, default=None):
        """page.evaluate, переживающий клиентские навигации (первое сообщение
        в свежем чате реroutит / → /c/<id>, контекст JS умирает на миг —
        см. подробности в claude_ui._safe_eval)."""
        for attempt in range(3):
            try:
                return await self.page.evaluate(script)
            except Exception as e:
                msg = str(e).lower()
                if "destroyed" not in msg and "navigation" not in msg:
                    return default
                if attempt == 2:
                    return default
                await asyncio.sleep(0.3)
        return default

    async def _stop_button_present(self) -> bool:
        return await self._safe_eval("""
            () => {
                const selectors = %s;
                for (const s of selectors) {
                    if (document.querySelector(s)) return true;
                }
                return false;
            }
        """ % json.dumps(STOP_SELECTORS_JS), default=False)

    async def wait_for_response(self, timeout: int | None = None, min_growth: int = 30,
                                is_cancelled=None) -> str:
        """Wait for ChatGPT to finish generating. No TOTAL time limit —
        `timeout` это idle-watchdog: сколько страница может НЕ меняться,
        прежде чем считаем её зависшей (стратегия 1-в-1 как у Claude:
        Stop-кнопка появилась → исчезла; ранний финиш и фолбэк — по
        приросту числа copy-кнопок; length-stability как последний рубеж).
        """
        def check_cancel():
            if is_cancelled and is_cancelled():
                raise ScenarioCancelled("Сценарий отменён пользователем")

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
                    f"ChatGPT не отвечает уже ~{_idle_limit}с — страница не меняется. "
                    "Возможно, chatgpt.com завис, упёрся в лимит сообщений или "
                    "изменил вёрстку — попробуйте ещё раз."
                )

        # Базлайн copy-кнопок: снятый send_message ДО отправки (быстрый ответ
        # мог уже догенерироваться), иначе — текущее состояние.
        if self._copies_before_send is not None:
            start_copies = self._copies_before_send
            self._copies_before_send = None
        else:
            start_copies = await self._copy_button_count()

        async def finished_via_copy() -> bool:
            return (await self._copy_button_count()) > start_copies

        # Phase 1: ждём появления Stop-кнопки (генерация началась), до 60с.
        started_deadline = asyncio.get_event_loop().time() + 60
        stop_seen = False
        while asyncio.get_event_loop().time() < started_deadline:
            check_cancel()
            if await self._stop_button_present():
                stop_seen = True
                break
            if await finished_via_copy():
                log.info("ChatGPT finished generating (copy button, before stop seen)")
                await asyncio.sleep(1)
                return await self._last_response_text()
            await asyncio.sleep(0.5)

        if stop_seen:
            # Phase 2: ждём исчезновения Stop-кнопки — без общего лимита.
            gone_checks = 0
            while True:
                check_cancel()
                await note_progress()
                check_deadline()
                if not await self._stop_button_present():
                    gone_checks += 1
                    if gone_checks >= 4:  # 2s stable absence
                        await asyncio.sleep(1)  # small settle
                        log.info("ChatGPT finished generating")
                        return await self._last_response_text()
                else:
                    gone_checks = 0
                await asyncio.sleep(0.5)

        # Fallback: stop-кнопку не увидели.
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
                log.info("ChatGPT finished generating (copy button, fallback)")
                await asyncio.sleep(1)
                return await self._last_response_text()
            cur_len = await self._safe_eval(
                "() => document.body.innerText.length", default=start_len)
            if cur_len - start_len > min_growth:
                if cur_len == prev_len:
                    stable += 1
                    if stable >= 60:  # 30с полной неподвижности
                        log.info("ChatGPT finished generating (length-based)")
                        return await self._last_response_text()
                else:
                    stable = 0
                    prev_len = cur_len
            await asyncio.sleep(0.5)

    async def _copy_button_count(self) -> int:
        """Число copy-кнопок у ответов АССИСТЕНТА. У chatgpt.com copy-кнопка
        есть и под сообщением пользователя (появляется сразу при отправке) —
        считать её нельзя, иначе «ответ готов» сработает до генерации.
        Скоупим по article-контейнеру хода диалога; фолбэк — общий счётчик."""
        return await self._safe_eval(
            """
            () => {
                const COPY = %s;
                const arts = document.querySelectorAll('article');
                if (arts.length) {
                    let n = 0;
                    for (const a of arts) {
                        if (a.querySelector('[data-message-author-role="assistant"]')
                            && a.querySelector(COPY)) n++;
                    }
                    return n;
                }
                return document.querySelectorAll(COPY).length;
            }
            """ % json.dumps(COPY_BUTTON_SELECTOR),
            default=0,
        )

    async def _last_response_text(self) -> str:
        # Scroll to bottom so virtualized content renders
        await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        # Strategy 1: copy-button click → clipboard (даёт чистый markdown)
        copy_buttons = await self.page.query_selector_all(COPY_BUTTON_SELECTOR)
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

        # Strategy 2: DOM scrape последнего сообщения ассистента. Берём
        # .markdown ВНУТРИ него — сводка «размышлений» (Thought for Xs)
        # рендерится отдельным блоком и в .markdown не попадает.
        text = await self.page.evaluate("""
            () => {
                const nodes = document.querySelectorAll(
                    '[data-message-author-role="assistant"]');
                if (!nodes.length) return null;
                const last = nodes[nodes.length - 1];
                const md = last.querySelector('.markdown');
                const txt = ((md || last).innerText || '').trim();
                return txt || null;
                // Фолбэка «самый длинный div» сознательно НЕТ — честная
                // ошибка дешевле тихо испорченного результата (см. claude_ui).
            }
        """)
        if text:
            log.info("_last_response_text: DOM scrape ok, len=%d", len(text))
            return text.strip()

        raise RuntimeError("Не удалось извлечь текст ответа ChatGPT")
