import asyncio
import base64
import collections
import hashlib
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime

from config import VERSION, VEO_IMAGE_MODELS, VEO_IMAGE_ASPECT_RATIOS, VEO_VIDEO_ASPECT_RATIOS, VEO_VIDEO_DURATIONS, USER_DATA_DIR
from modules.logger import get as get_logger, LOG_FILE

log = get_logger("vizo.api")
from modules import settings, updater, veo_api
from modules import voices as voices_mod
from modules import languages as languages_mod
from modules import scenarios as scenarios_mod
from modules.transcript import get_transcript, get_title, get_description, download_thumbnail, extract_video_id
from modules.voice_api import synthesize, VoiceCancelled
from modules.voice_templates import resolve_lang
from modules.claude_ui import ClaudeAutomation
from modules.gpt_ui import GPTAutomation
from modules.veo_api import generate_images


def _safe_name(text: str, max_len: int = 50) -> str:
    return "".join(c for c in text if c.isalnum() or c in " -_").strip()[:max_len]


# Контекст потока пайплайна: каждый pipeline-поток несёт свой run_id.
# _emit раньше штамповал события ТЕКУЩИМ self._active_run_id — события
# старого (отменённого) запуска получали id нового и пролезали в его UI.
_run_ctx = threading.local()


# Step fields that hold file paths (filesystem references).
# (step_type, field_name, kind) — kind is "list" (a list of strings) or "single" (one string).
_PATH_FIELDS = (
    ("claude_prompt", "file_paths", "list"),
    ("gpt_prompt",    "file_paths", "list"),
    ("video_i2v",     "image_path", "single"),
    ("video_batch",   "start_image_path", "single"),
    ("video_batch",   "end_image_path",   "single"),
)


def _safe_filename(name: str) -> str:
    """Filesystem-safe filename, keeping extension."""
    if not name:
        return "file"
    base = os.path.basename(name)
    base = re.sub(r"[^\w. \-]", "_", base, flags=re.UNICODE)
    return base[:120] or "file"


def _try_inline_file(value, files_out: dict):
    """If `value` is a real filesystem path, read it, register under a hash, return a {"$file": h} ref.
    Otherwise return None (caller keeps original value)."""
    if not isinstance(value, str) or not value:
        return None
    if "{" in value:
        return None  # variable / template, leave as-is
    if not os.path.isfile(value):
        return None
    try:
        with open(value, "rb") as f:
            data = f.read()
    except Exception:
        return None
    h = hashlib.sha1(data).hexdigest()[:16]
    if h not in files_out:
        files_out[h] = {
            "name": os.path.basename(value),
            "size": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        }
    return {"$file": h}


def _inline_scenario_files(scenario: dict, files_out: dict) -> None:
    """Walk all path-bearing fields in steps; replace concrete paths with $file refs.
    Mutates `scenario` and `files_out` in place."""
    for step in scenario.get("steps", []):
        t = step.get("type")
        for step_type, field, kind in _PATH_FIELDS:
            if t != step_type:
                continue
            if kind == "list":
                items = step.get(field) or []
                if not isinstance(items, list):
                    continue
                new_items = []
                for p in items:
                    ref = _try_inline_file(p, files_out)
                    new_items.append(ref if ref is not None else p)
                step[field] = new_items
            else:  # single
                ref = _try_inline_file(step.get(field), files_out)
                if ref is not None:
                    step[field] = ref


def _materialize_ref(value, files_index: dict, target_dir: str):
    """If value is a {"$file": h} ref, write the file out and return the new local path. Otherwise None."""
    if not isinstance(value, dict) or "$file" not in value:
        return None
    h = value.get("$file")
    info = files_index.get(h) if isinstance(files_index, dict) else None
    if not info or "data" not in info:
        return None
    # Префикс хэша: два разных файла с одинаковым базовым именем (1.png из
    # разных папок отправителя) не должны затирать друг друга при импорте.
    target = os.path.join(target_dir, f"{h}_{_safe_filename(info.get('name') or 'file')}")
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(target, "wb") as f:
            f.write(base64.b64decode(info["data"]))
        return target
    except Exception:
        return None


def _materialize_scenario_files(scenario: dict, files_index: dict, target_dir: str) -> int:
    """Walk all path-bearing fields; replace $file refs with paths to extracted local files.
    Returns count of files written. Mutates scenario in place."""
    written = 0
    for step in scenario.get("steps", []):
        t = step.get("type")
        for step_type, field, kind in _PATH_FIELDS:
            if t != step_type:
                continue
            if kind == "list":
                items = step.get(field) or []
                if not isinstance(items, list):
                    continue
                new_items = []
                for p in items:
                    local = _materialize_ref(p, files_index, target_dir)
                    if local:
                        new_items.append(local); written += 1
                    else:
                        new_items.append(p)
                step[field] = new_items
            else:
                local = _materialize_ref(step.get(field), files_index, target_dir)
                if local:
                    step[field] = local; written += 1
    return written


# ----------------------------------------------------------------------------
# Run queue
# ----------------------------------------------------------------------------
# Every pipeline (scenario / translate / preset / generation) drives the SAME
# single Claude browser (one Chrome profile, hard-locked) and the OS clipboard,
# so runs MUST execute strictly one at a time. A _Run captures everything needed
# to (a) start the pipeline later, (b) rebuild its status screen when the user
# leaves and comes back, and (c) place it in the queue list.
class _Run:
    def __init__(self, run_id, kind, title, subtitle, icon, steps, step_meta, fn, args):
        self.run_id = run_id
        self.kind = kind                  # scenario | translate | preset | generation
        self.title = title or "Запуск"
        self.subtitle = subtitle or ""
        self.icon = icon or ""
        self.steps = list(steps or [])    # list[str] — step labels for the UI list
        self.step_meta = list(step_meta or [])  # list[{type, parallel_with_previous}]
        self.fn = fn                      # bound coroutine function
        self.args = args                  # tuple of args
        self.cancel_event = threading.Event()
        self.thread = None
        self.runner = None                # ScenarioRunner (scenario kind) for skip/cancel
        self.output_dir = None
        # Имя папки результата, заданное пользователем при запуске (или None —
        # автоимя). Нужно, чтобы различать одинаковые пайплайны в очереди.
        self.folder_name = None
        # Ссылка на YouTube (если запуск с ней стартовал) и id сценария —
        # для проверки дублей «та же ссылка в том же пайплайне».
        self.url = None
        self.scenario_id = None
        # Название ролика с YouTube — подтягивается фоном после постановки в
        # очередь, чтобы карточка говорила о видео больше, чем голая ссылка.
        self.video_title = None
        # status: queued | running | waiting_input | done | error | cancelled
        self.status = "queued"
        # live progress snapshot (so the UI can repaint on return)
        self.step_text = "В очереди…"
        self.detail = ""
        self.percent = 0
        self.index = 0
        self.pending_input = None         # {message, kind, title} while waiting for user
        self.error = None                 # {message, traceback, log_file, failed_step_idx, report_path}
        self.cancelled_at_step = None
        # Auto-retry bookkeeping (set from settings at creation).
        self.attempt = 1                  # 1 = first try; 2 = first auto-retry; ...
        self.retries_left = 0
        # История между сессиями: когда завершился и восстановлен ли из файла.
        self.finished_at = None           # isoformat либо None
        self.restored = False             # True — загружен из run_history.json
        # «Продолжить с места»: пайплайн должен переиспользовать output_dir
        # и подхватить чекпоинт вместо старта с нуля.
        self.resume_requested = False

    def public(self):
        """Plain dict pushed to JS (no thread/event/callable internals)."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "title": self.title,
            "subtitle": self.subtitle,
            "icon": self.icon,
            "steps": self.steps,
            "step_meta": self.step_meta,
            "status": self.status,
            "step_text": self.step_text,
            "detail": self.detail,
            "percent": self.percent,
            "index": self.index,
            "pending_input": self.pending_input,
            "error": self.error,
            "output_dir": self.output_dir,
            "folder_name": self.folder_name,
            "url": self.url,
            "scenario_id": self.scenario_id,
            "video_title": self.video_title,
            "cancelled_at_step": self.cancelled_at_step,
            "attempt": self.attempt,
            "finished_at": self.finished_at,
            "restored": self.restored,
            "resumable": self.is_resumable(),
        }

    def is_resumable(self) -> bool:
        """Можно ли продолжить этот запуск с места падения (есть чекпоинт /
        готовые артефакты в его папке)."""
        if self.status not in ("error", "cancelled"):
            return False
        if self.fn is None or self.args is None or not self.output_dir:
            return False
        if self.kind == "scenario":
            return os.path.exists(os.path.join(
                self.output_dir, scenarios_mod.CHECKPOINT_FILENAME))
        if self.kind == "translate":
            # Сценарий перевода уже получен от Claude — самая дорогая часть.
            path = os.path.join(self.output_dir, "script.txt")
            try:
                return os.path.getsize(path) > 0
            except OSError:
                return False
        return False


class Api:
    """Bridge between HTML/JS and Python. Methods here are callable as window.pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._claude = None
        self._current_task = None
        self._pending_reply = None  # user's reply to Claude during tartaria flow
        # Run tracking — every pipeline run gets a unique id so stale events
        # from a cancelled run can't mess with a new run's UI.
        self._run_id = 0
        self._active_run_id = None
        self._cancel_event = None  # threading.Event for the active run
        self._active_thread = None
        self._active_runner = None
        # Run queue: pipelines execute one at a time. _runs is the registry of
        # every run this session (keyed by id, insertion-ordered); _queue holds
        # the ids still waiting to start. Guarded by _qlock because enqueue is
        # called from the JS bridge thread while runs finish on worker threads.
        self._runs = {}                       # run_id -> _Run
        self._queue = collections.deque()     # run_ids waiting to start
        self._queue_paused = False            # set True when the user cancels the active run
        self._qlock = threading.RLock()
        self._batch_ids = set()               # runs since the queue was last idle (for the "done" notification)
        self._title_cache = {}                # url -> название ролика (для карточек очереди)
        # История завершённых запусков переживает перезапуск приложения.
        self._restore_history()

    def set_window(self, window):
        self._window = window

    # --- JS → Python UI helpers ---

    def _emit(self, event: str, data=None):
        """Call a JS function window.onPyEvent(event, data).

        Pipeline events (progress, scenario_start, error, done, cancelled,
        user_input_request, claude_asks) are tagged with the current run_id
        so the UI can route them to the right run. The same events also update
        the run's server-side snapshot, so the user can leave the status screen
        and rebuild it later from get_run_state().
        """
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            data = {"value": data}
        # Stamp pipeline events with THIS thread's run id (set in the run worker
        # thread). Fallback to the active id for non-pipeline emits.
        rid = getattr(_run_ctx, "run_id", None)
        if rid is None:
            rid = self._active_run_id
        if rid is not None and "run_id" not in data:
            data["run_id"] = rid
        # Keep the server-side run snapshot in sync (and write an error report
        # on failure) BEFORE pushing to JS — so a re-fetch right after sees it.
        run = self._runs.get(rid) if rid is not None else None
        if run is not None:
            self._update_run_snapshot(run, event, data)
        if not self._window:
            return
        import json as _json
        payload = _json.dumps(data)
        self._window.evaluate_js(f"window.onPyEvent && window.onPyEvent('{event}', {payload})")

    # status transitions that warrant re-pushing the whole queue snapshot to JS
    _QUEUE_REFRESH_EVENTS = frozenset((
        "scenario_start", "user_input_request", "done", "error", "cancelled",
    ))

    def _update_run_snapshot(self, run, event, data):
        """Fold a pipeline event into the run's stored progress snapshot."""
        if event == "progress":
            if data.get("step"):
                run.step_text = data["step"]
            if data.get("percent") is not None:
                run.percent = data["percent"]
            if data.get("index") is not None:
                run.index = data["index"]
            if run.status == "waiting_input":
                run.status = "running"
            run.pending_input = None
        elif event == "progress_detail":
            run.detail = data.get("detail") or ""
        elif event == "scenario_start":
            sts = data.get("steps") or []
            if sts:
                run.steps = [s.get("name") or s.get("type") for s in sts]
            if run.status == "queued":
                run.status = "running"
        elif event == "user_input_request":
            run.status = "waiting_input"
            run.pending_input = {
                "message": data.get("message") or "",
                "kind": data.get("kind") or "claude",
                "title": data.get("title"),
            }
        elif event == "done":
            run.status = "done"
            run.percent = 100
            run.step_text = "Готово"
            run.detail = ""
            run.pending_input = None
            if data.get("output_dir"):
                run.output_dir = data["output_dir"]
        elif event == "cancelled":
            run.status = "cancelled"
            run.step_text = "Отменено"
            run.pending_input = None
            if data.get("cancelled_at_step") is not None:
                run.cancelled_at_step = data.get("cancelled_at_step")
            if data.get("output_dir"):
                run.output_dir = data["output_dir"]
        elif event == "error":
            run.status = "error"
            run.step_text = "Ошибка"
            run.pending_input = None
            msg = data.get("message") or "Ошибка"
            tb = data.get("traceback") or ""
            report_path = self._write_error_report(run, msg, tb, data)
            run.error = {
                "message": msg,
                "traceback": tb,
                "log_file": data.get("log_file") or LOG_FILE,
                "failed_step_idx": data.get("failed_step_idx"),
                "report_path": report_path,
            }
            if data.get("output_dir") and not run.output_dir:
                run.output_dir = data["output_dir"]
            # Let the UI surface/open the report file.
            data.setdefault("report_path", report_path)
            data.setdefault("output_dir", run.output_dir or "")
        # Завершённый запуск — в историю (переживает перезапуск приложения).
        if event in ("done", "cancelled", "error"):
            run.finished_at = datetime.now().isoformat(timespec="seconds")
            self._save_history()
        # Reflect meaningful status changes in the queue list immediately.
        if event in self._QUEUE_REFRESH_EVENTS:
            self._emit_queue()

    def _write_error_report(self, run, message, tb, data):
        """Write a human-readable crash report into the run's output folder so
        the user finds out what went wrong next to the produced files. If the
        run failed before any folder existed, create one so a report still lands."""
        try:
            out = run.output_dir or data.get("output_dir")
            if not out:
                out = self._create_output_dir(run.title or "run", suffix=" ОШИБКА")
                run.output_dir = out
            os.makedirs(out, exist_ok=True)
            path = os.path.join(out, "ОШИБКА.txt")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            step_no = data.get("failed_step_idx")
            if step_no is not None and 0 <= step_no < len(run.steps):
                step_line = f"Шаг:      {step_no + 1}/{len(run.steps)} — {run.steps[step_no]}\n"
            elif step_no is not None:
                step_line = f"Шаг:      {step_no + 1}\n"
            else:
                step_line = ""
            src_line = f"Источник: {run.subtitle}\n" if run.subtitle else ""
            text = (
                "Отчёт об ошибке vizo\n"
                "=====================\n\n"
                f"Время:    {ts}\n"
                f"Версия:   {VERSION}\n"
                f"Запуск:   {run.title}\n"
                f"Тип:      {run.kind}\n"
                f"{src_line}"
                f"{step_line}"
                f"\nОшибка:\n{message}\n"
                f"\nПодробности:\n{tb or '—'}\n"
                f"\nЛог-файл: {data.get('log_file') or LOG_FILE}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log.info("Error report written: %s", path)
            return path
        except Exception:
            log.exception("Failed to write error report")
            return None

    def _progress(self, step: str, percent: int = None, index: int = None):
        self._emit("progress", {"step": step, "percent": percent, "index": index})

    # --- Settings ---

    def load_settings(self):
        return settings.load()

    def save_settings(self, data: dict):
        settings.save(data)
        return {"ok": True}

    def pick_folder(self):
        import webview
        if not self._window:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def pick_image_file(self):
        import webview
        if not self._window:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",),
        )
        if not result:
            return None
        path = result[0]
        return {"path": path, "name": os.path.basename(path)}

    def pick_image_files(self):
        import webview
        if not self._window:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",),
        )
        if not result:
            return []
        return [{"path": p, "name": os.path.basename(p)} for p in result]

    def pick_icon_image(self):
        """Open file dialog, read selected image, return as base64 data URL.
        Used for scenario icons so the image is self-contained in the JSON."""
        import webview
        if not self._window:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.svg)",),
        )
        if not result:
            return None
        path = result[0]
        try:
            with open(path, "rb") as f:
                content = f.read()
            if len(content) > 512 * 1024:
                return {"ok": False, "error": "Файл больше 512 КБ — выбери картинку поменьше"}
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
            b64 = base64.b64encode(content).decode("ascii")
            return {"ok": True, "data_url": f"data:{mime};base64,{b64}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- Updates ---

    def get_version(self):
        return {"version": VERSION}

    def check_updates(self):
        return updater.check_for_updates()

    def apply_update(self, url: str):
        return updater.download_and_apply(url)

    # --- Claude auth (opens browser for login) ---

    def link_claude(self):
        async def _link():
            claude = ClaudeAutomation()
            try:
                # start() now blocks until the user has logged in (it polls the
                # page URL) instead of reading stdin — so reaching the end means
                # linking succeeded. The persistent Chrome profile keeps the
                # session for later runs.
                await claude.start(status_cb=lambda m: self._emit(
                    "claude_link_status", {"message": m}))
            finally:
                await claude.close()

        def run():
            try:
                asyncio.run(_link())
                self._emit("claude_linked", {"ok": True})
            except Exception as e:
                self._emit("claude_linked", {"ok": False, "error": str(e)})
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def link_gpt(self):
        """Открыть браузер ChatGPT для входа в аккаунт (аналог link_claude).
        Логин НЕ обязателен: chatgpt.com работает и анонимно, но аккаунт даёт
        выбор моделей, вложения и нормальные лимиты."""
        async def _link():
            gpt = GPTAutomation()
            try:
                await gpt.start(status_cb=lambda m: self._emit(
                    "gpt_link_status", {"message": m}))
                # start() не ждёт логина, если chatgpt.com пустил анонимом, —
                # держим окно открытым, пока пользователь входит в аккаунт.
                # Признак входа: кнопка «Log in» исчезла из шапки.
                self._emit("gpt_link_status", {
                    "message": "Окно открыто. Войди в аккаунт ChatGPT "
                               "(или закрой окно, чтобы работать без входа)"})
                deadline = asyncio.get_event_loop().time() + 300
                while asyncio.get_event_loop().time() < deadline:
                    if not gpt.is_alive():
                        break  # пользователь закрыл окно — ок, аноним-режим
                    try:
                        n = await gpt.page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                   'button, a')).filter(b =>
                                   /^log ?in$/i.test((b.innerText || '').trim())
                               ).length"""
                        )
                        if not n:
                            break  # кнопки Log in нет — залогинен
                    except Exception:
                        pass
                    await asyncio.sleep(2)
            finally:
                await gpt.close()

        def run():
            try:
                asyncio.run(_link())
                self._emit("gpt_linked", {"ok": True})
            except Exception as e:
                self._emit("gpt_linked", {"ok": False, "error": str(e)})
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # --- Pipeline runners ---

    def start_translate(self, url: str, language: str, folder_name: str = None):
        steps = ["Извлечь транскрипт", "Получить заголовок", "Запустить Claude",
                 "Перевод", "SEO", "Превью", "Озвучка"]
        folder = (folder_name or "").strip() or None
        run = self._new_run("translate", f"Перевод — {language}",
                            f"{folder} — {url}" if folder else url, "🌐",
                            steps, [], self._pipeline_translate, (url, language))
        run.folder_name = folder
        run.url = (url or "").strip() or None
        self._fetch_video_title(run)
        return self._enqueue(run)

    def start_preset(self, url: str, preset: str, generate_images_flag: bool = True,
                     folder_name: str = None):
        steps = ["Заголовок", "Транскрипт", "Запустить Claude", "Сценарий",
                 "Картинки + Озвучка" if generate_images_flag else "Озвучка", "Готово"]
        run = self._new_run("preset", "Тартария", url, "🏛️",
                            steps, [], self._pipeline_preset,
                            (url, preset, generate_images_flag))
        run.folder_name = (folder_name or "").strip() or None
        return self._enqueue(run)

    # --- Standalone Generation (VeoNonStop) ---

    def get_generation_options(self):
        """Возвращает списки доступных моделей/соотношений для UI."""
        return {
            "image_models": VEO_IMAGE_MODELS,
            "image_aspect_ratios": VEO_IMAGE_ASPECT_RATIOS,
            "video_aspect_ratios": VEO_VIDEO_ASPECT_RATIOS,
            "video_durations": VEO_VIDEO_DURATIONS,
            "video_counts": [1, 2, 3, 4],
        }

    def start_generation(self, mode: str, params: dict):
        """Универсальный запуск генерации.

        mode: 'banana_image' | 'text_to_video' | 'image_to_video' |
              'multi_image' | 'batch_frame'
        params зависят от режима — см. _gen_*.
        """
        steps = (["Генерация", "Скачать", "Готово"] if mode == "banana_image"
                 else ["Создать задачу", "Генерация", "Скачать", "Готово"])
        labels = {
            "banana_image": "Картинки (Banana)",
            "text_to_video": "Видео: текст→видео",
            "image_to_video": "Видео: картинка→видео",
            "multi_image": "Видео: мульти-кадр",
            "batch_frame": "Видео: переход кадров",
        }
        run = self._new_run("generation", labels.get(mode, f"Генерация — {mode}"),
                            "", "🎬", steps, [], self._pipeline_generation, (mode, params))
        return self._enqueue(run)

    def cancel_video_task(self, task_id: str):
        try:
            return {"ok": True, "data": veo_api.cancel_task(task_id)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def submit_claude_reply(self, text: str):
        self._pending_reply = text
        return {"ok": True}

    # --- Voices CRUD ---

    def list_voices(self):
        try:
            return voices_mod.load_all()
        except Exception as e:
            return {"error": str(e)}

    def save_voice(self, voice: dict):
        try:
            saved = voices_mod.save(voice)
            return {"ok": True, "voice": saved}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_voice(self, voice_id: str):
        ok = voices_mod.delete(voice_id)
        return {"ok": ok}

    def duplicate_voice(self, voice_id: str):
        v = voices_mod.duplicate(voice_id)
        return {"ok": bool(v), "voice": v}

    def restore_voice_defaults(self):
        n = voices_mod.restore_defaults()
        return {"ok": True, "restored": n}

    def restore_scenario_defaults(self):
        n = scenarios_mod.restore_defaults()
        return {"ok": True, "restored": n}

    def test_voice(self, voice: dict, text: str):
        """Synthesize a short test snippet with given (unsaved) voice
        settings and return path to the result mp3."""
        try:
            out_dir = os.path.join(settings.load().get("output_dir") or settings.DEFAULT_OUTPUT_DIR, "_voice_test")
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(out_dir, f"test_{ts}.mp3")

            def run():
                try:
                    synthesize(text or "Привет, это тест голоса.", voice, path)
                    self._emit("voice_test_done", {"path": path})
                except Exception as e:
                    self._emit("voice_test_error", {"message": str(e)})

            threading.Thread(target=run, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def voice_usage(self, voice_id: str):
        """Count where this voice is referenced (languages + scenarios)."""
        try:
            uses = []
            for l in languages_mod.load_all():
                if l.get("voice_id") == voice_id:
                    uses.append({"kind": "language", "name": l.get("name") or l.get("id")})
            for s in scenarios_mod.load_all():
                for step in s.get("steps", []):
                    if step.get("type") == "voice" and step.get("preset") == voice_id:
                        uses.append({"kind": "scenario", "name": s.get("name") or s.get("id")})
                        break
            return {"ok": True, "uses": uses, "count": len(uses)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- Languages CRUD ---

    def list_languages(self):
        try:
            return languages_mod.load_all()
        except Exception as e:
            return {"error": str(e)}

    def save_language(self, lang: dict):
        try:
            return {"ok": True, "lang": languages_mod.save(lang)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_language(self, lang_id: str):
        return {"ok": languages_mod.delete(lang_id)}

    def reorder_languages(self, ordered_ids: list):
        languages_mod.reorder(ordered_ids)
        return {"ok": True}

    def restore_language_defaults(self):
        n = languages_mod.restore_defaults()
        return {"ok": True, "restored": n}

    # --- Scenarios CRUD ---

    def list_scenarios(self):
        try:
            return scenarios_mod.load_all()
        except Exception as e:
            return {"error": str(e)}

    def get_scenario(self, scenario_id: str):
        return scenarios_mod.get(scenario_id)

    def save_scenario(self, scenario: dict):
        try:
            return {"ok": True, "scenario": scenarios_mod.save(scenario)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_scenario(self, scenario_id: str):
        return {"ok": scenarios_mod.delete(scenario_id)}

    def duplicate_scenario(self, scenario_id: str):
        s = scenarios_mod.duplicate(scenario_id)
        return {"ok": bool(s), "scenario": s}

    # --- Scenario export / import ---

    def build_export(self, scenario_id: str):
        """Build a self-contained payload for a scenario.

        Includes referenced voices and inlines local files (claude examples, image refs).
        Absolute paths are scrubbed — receivers never see the sender's filesystem layout."""
        import json as _json
        sc = scenarios_mod.get(scenario_id)
        if not sc:
            return {"ok": False, "error": "Сценарий не найден"}
        # Deep-copy so we don't mutate the persisted scenario
        sc_clean = _json.loads(_json.dumps(sc))
        # Strip runtime/identifier fields
        for k in ("id", "last_run_at", "builtin"):
            sc_clean.pop(k, None)
        # Inline file paths → {"$file": hash} refs, collecting bytes in `files`
        files: dict = {}
        _inline_scenario_files(sc_clean, files)
        # Collect voice IDs referenced from voice steps
        voice_ids = set()
        for step in sc.get("steps", []):
            if step.get("type") == "voice" and step.get("preset"):
                voice_ids.add(step["preset"])
        voices_by_id = {v.get("id"): v for v in voices_mod.load_all()}
        voices = [voices_by_id[vid] for vid in voice_ids if vid in voices_by_id]
        return {
            "ok": True,
            "payload": {
                "vizo_version": 1,
                "exported_at": datetime.now().isoformat(),
                "scenario": sc_clean,
                "voices": voices,
                "files": files,
            },
            "suggested_name": sc.get("name") or "scenario",
            "file_count": len(files),
        }

    def save_export_file(self, suggested_name: str, payload: dict):
        import webview
        import json as _json
        if not self._window:
            return {"ok": False, "error": "no window"}
        safe = _safe_name(suggested_name or "scenario") or "scenario"
        default_name = f"{safe}.vizo.json"
        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("vizo scenario (*.json)",),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_import_file(self):
        """Open file dialog, parse JSON, return parsed payload + conflict info."""
        import webview
        import json as _json
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("vizo scenario (*.json)", "All files (*.*)"),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            with open(path, encoding="utf-8") as f:
                payload = _json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"Не удалось прочитать файл: {e}"}
        if not isinstance(payload, dict) or "scenario" not in payload:
            return {"ok": False, "error": "Это не файл сценария vizo"}
        sc = payload.get("scenario") or {}
        if not isinstance(sc, dict) or not isinstance(sc.get("steps"), list):
            return {"ok": False, "error": "В файле нет данных сценария"}
        # Name conflict check
        name = sc.get("name") or "Без названия"
        conflict_id = None
        for ex in scenarios_mod.load_all():
            if ex.get("name") == name:
                conflict_id = ex.get("id")
                break
        # Missing voices check
        existing_voice_ids = {v.get("id") for v in voices_mod.load_all()}
        incoming_voices = payload.get("voices") or []
        missing_voices = [
            {"id": v.get("id"), "name": v.get("name") or v.get("id")}
            for v in incoming_voices
            if v.get("id") and v.get("id") not in existing_voice_ids
        ]
        # File bundle stats
        files = payload.get("files") or {}
        file_count = len(files) if isinstance(files, dict) else 0
        file_total_size = 0
        if isinstance(files, dict):
            for finfo in files.values():
                try:
                    file_total_size += int(finfo.get("size", 0))
                except Exception:
                    pass
        return {
            "ok": True,
            "payload": payload,
            "name": name,
            "icon": sc.get("icon") or "🔗",
            "description": sc.get("description") or "",
            "step_count": len(sc.get("steps", [])),
            "conflict_id": conflict_id,
            "missing_voices": missing_voices,
            "file_count": file_count,
            "file_total_size": file_total_size,
        }

    def commit_import(self, payload: dict, options: dict = None):
        """Persist imported scenario (and optionally voices).

        options:
          mode: "copy" | "replace"
          conflict_id: id of existing scenario to replace (when mode=replace)
          import_voices: True/False — add missing voices to collection
        """
        options = options or {}
        mode = options.get("mode") or "copy"
        import_voices = options.get("import_voices", True)
        conflict_id = options.get("conflict_id")

        sc_src = payload.get("scenario") if isinstance(payload, dict) else None
        if not isinstance(sc_src, dict):
            return {"ok": False, "error": "Пустой сценарий"}
        sc = dict(sc_src)
        sc.pop("id", None)
        sc.pop("last_run_at", None)
        sc.pop("builtin", None)  # imported is never builtin
        sc.pop("pinned", None)   # don't inherit pin state

        # Voice imports (skip ones already present by id)
        voice_count = 0
        if import_voices:
            existing_ids = {v.get("id") for v in voices_mod.load_all()}
            for v in (payload.get("voices") or []):
                vid = v.get("id")
                if not vid or vid in existing_ids:
                    continue
                v_clean = dict(v)
                v_clean["builtin"] = False
                try:
                    voices_mod.save(v_clean)
                    voice_count += 1
                except Exception:
                    pass

        if mode == "replace" and conflict_id:
            sc["id"] = conflict_id
        elif conflict_id:
            # Copy with name suffix to distinguish from existing
            sc["name"] = (sc.get("name") or "Без названия") + " (импорт)"
        try:
            saved = scenarios_mod.save(sc)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # Materialize bundled files now that we have an ID
        files_index = payload.get("files") or {}
        files_written = 0
        if isinstance(files_index, dict) and files_index:
            target_dir = os.path.join(USER_DATA_DIR, "imported_assets", saved.get("id") or "")
            files_written = _materialize_scenario_files(saved, files_index, target_dir)
            if files_written:
                try:
                    saved = scenarios_mod.save(saved)
                except Exception:
                    pass
        return {
            "ok": True,
            "scenario": saved,
            "voices_imported": voice_count,
            "files_written": files_written,
        }

    def validate_scenario(self, scenario: dict):
        return {"errors": scenarios_mod.validate(scenario)}

    def run_scenario(self, scenario_id: str, starting_vars: dict = None,
                     folder_name: str = None):
        """Enqueue a scenario run. It starts immediately if nothing else is
        running, otherwise it waits in the queue and auto-starts in turn.

        folder_name — пользовательское имя папки результата (см. настройку
        ask_folder_name); показывается подзаголовком в очереди, чтобы
        различать несколько запусков одного сценария."""
        scenario = scenarios_mod.get(scenario_id)
        if not scenario:
            return {"ok": False, "error": "Сценарий не найден"}
        steps = [s.get("name") or s.get("type") for s in scenario.get("steps", [])]
        step_meta = [
            {"type": s.get("type"),
             "parallel_with_previous": bool(s.get("parallel_with_previous"))}
            for s in scenario.get("steps", [])
        ]
        folder = (folder_name or "").strip() or None
        run = self._new_run(
            "scenario", scenario.get("name") or "Сценарий", folder or "",
            scenario.get("icon") or "", steps, step_meta,
            self._pipeline_scenario, (scenario, starting_vars or {}),
        )
        run.folder_name = folder
        run.scenario_id = scenario.get("id")
        # URL из заранее введённых переменных (см. runScenarioWithInputs):
        # значение любого yt_url-шага. Нужен для заголовка ролика и дублей.
        for v in (starting_vars or {}).values():
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                run.url = v.strip()
                break
        self._fetch_video_title(run)
        return self._enqueue(run)

    # --- Queue management ---

    def _new_run(self, kind, title, subtitle, icon, steps, step_meta, fn, args):
        with self._qlock:
            self._run_id += 1
            run = _Run(self._run_id, kind, title, subtitle, icon, steps, step_meta, fn, args)
        try:
            run.retries_left = max(0, int(settings.load().get("auto_retry", 1)))
        except Exception:
            run.retries_left = 1
        return run

    def _enqueue(self, run):
        with self._qlock:
            self._runs[run.run_id] = run
            self._queue.append(run.run_id)
            self._batch_ids.add(run.run_id)
            # Explicitly starting a run is a "go" — lift a pause left by an
            # earlier cancel so this run (and anything still queued) proceeds.
            self._queue_paused = False
        self._maybe_start_next()
        self._emit_queue()
        return {
            "ok": True,
            "run_id": run.run_id,
            "started": (self._active_run_id == run.run_id),
            "position": self._queue_position(run.run_id),
        }

    # --- История запусков (переживает перезапуск приложения) ---

    _HISTORY_FILE = os.path.join(USER_DATA_DIR, "run_history.json")
    _HISTORY_LIMIT = 100          # хранить не больше N завершённых запусков
    _HISTORY_ARGS_MAX = 300_000   # не тащить в файл гигантские args (b64-картинки)
    _TERMINAL = ("done", "error", "cancelled")

    def _history_entry(self, run) -> dict:
        """Сериализовать завершённый запуск для run_history.json.

        args сохраняются, если они JSON-сериализуемы и компактны — тогда
        «Возобновить» работает и после перезапуска приложения (fn восстановится
        по kind). Трейсбек в файл не пишем: полный отчёт и так лежит в папке."""
        import json as _json
        e = run.public()
        e.pop("pending_input", None)
        if e.get("error"):
            err = e["error"]
            e["error"] = {"message": err.get("message"),
                          "report_path": err.get("report_path"),
                          "log_file": err.get("log_file")}
        try:
            args_json = _json.dumps(run.args, ensure_ascii=False)
            if len(args_json) <= self._HISTORY_ARGS_MAX:
                e["args"] = _json.loads(args_json)
        except Exception:
            pass
        return e

    def _save_history(self):
        """Атомарно переписать run_history.json текущими завершёнными запусками.
        Любая ошибка здесь не должна ломать пайплайн — история вторична."""
        import json as _json
        try:
            with self._qlock:
                entries = [self._history_entry(r) for r in self._runs.values()
                           if r.status in self._TERMINAL]
            entries = entries[-self._HISTORY_LIMIT:]
            tmp = self._HISTORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(entries, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._HISTORY_FILE)
        except Exception:
            log.exception("Не смог сохранить историю запусков")

    def _restore_history(self):
        """Загрузить завершённые запуски прошлых сессий в реестр очереди.

        fn восстанавливается по kind — поэтому «Возобновить» работает и для
        восстановленных запусков (если args сохранились). Битый файл истории
        молча пропускается: это кэш, а не данные."""
        import json as _json
        try:
            if not os.path.exists(self._HISTORY_FILE):
                return
            with open(self._HISTORY_FILE, encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, list):
                return
        except Exception:
            log.exception("Не смог прочитать историю запусков")
            return
        fns = {
            "scenario": self._pipeline_scenario,
            "translate": self._pipeline_translate,
            "preset": self._pipeline_preset,
            "generation": self._pipeline_generation,
        }
        for e in data[-self._HISTORY_LIMIT:]:
            try:
                if not isinstance(e, dict) or e.get("status") not in self._TERMINAL:
                    continue
                args = e.get("args")
                fn = fns.get(e.get("kind")) if isinstance(args, list) else None
                self._run_id += 1
                run = _Run(self._run_id, e.get("kind") or "scenario",
                           e.get("title"), e.get("subtitle"), e.get("icon"),
                           e.get("steps") or [], e.get("step_meta") or [],
                           fn, tuple(args) if isinstance(args, list) else None)
                run.status = e["status"]
                run.step_text = e.get("step_text") or {
                    "done": "Готово", "error": "Ошибка", "cancelled": "Отменено",
                }[e["status"]]
                run.percent = e.get("percent") or (100 if e["status"] == "done" else 0)
                run.output_dir = e.get("output_dir")
                run.folder_name = e.get("folder_name")
                run.url = e.get("url")
                run.scenario_id = e.get("scenario_id")
                run.video_title = e.get("video_title")
                run.error = e.get("error")
                run.cancelled_at_step = e.get("cancelled_at_step")
                run.finished_at = e.get("finished_at")
                run.restored = True
                self._runs[run.run_id] = run
            except Exception:
                continue

    def clear_history(self):
        """Убрать из списка все завершённые запуски (активные и очередь не трогаем)."""
        with self._qlock:
            for rid in [rid for rid, r in self._runs.items()
                        if r.status in self._TERMINAL]:
                self._runs.pop(rid, None)
        self._save_history()
        self._emit_queue()
        return {"ok": True}

    def find_duplicate(self, url: str, scenario_id: str = None, language: str = None):
        """Есть ли уже запуск с этой же ссылкой В ТОМ ЖЕ пайплайне.

        Та же ссылка в другом сценарии — это норма (пользователь гоняет одно
        видео через разные пайплайны), поэтому сравниваем только внутри одного
        сценария (scenario_id) или одного языка перевода (language). Ссылки
        сводятся к video id: youtu.be/X и youtube.com/watch?v=X — одно видео.
        Возвращает данные самого свежего совпадения — UI показывает
        предупреждение с кнопкой «запустить всё равно», это не запрет."""
        u = (url or "").strip()
        if not u:
            return {"found": False}
        vid = extract_video_id(u)

        def same_url(other):
            if not other:
                return False
            if vid:
                return extract_video_id(other) == vid
            return other.strip() == u

        best = None
        with self._qlock:
            for r in self._runs.values():
                if scenario_id is not None:
                    if r.scenario_id != scenario_id:
                        continue
                elif language is not None:
                    if r.kind != "translate" or r.title != f"Перевод — {language}":
                        continue
                else:
                    continue
                if same_url(r.url) and (best is None or r.run_id > best.run_id):
                    best = r
        if best is None:
            return {"found": False}
        return {
            "found": True,
            "run_id": best.run_id,
            "status": best.status,
            "title": best.title,
            "video_title": best.video_title,
            "folder_name": best.folder_name,
        }

    @staticmethod
    def _copy_run_identity(src, dst):
        """Перенести пользовательские атрибуты запуска на его клон
        (авто-повтор / «Возобновить»)."""
        dst.folder_name = src.folder_name
        dst.url = src.url
        dst.scenario_id = src.scenario_id
        dst.video_title = src.video_title

    def _fetch_video_title(self, run):
        """Подтянуть название YouTube-ролика фоном и обновить карточку очереди.

        Голая ссылка в карточке ни о чём не говорит, когда в очереди много
        запусков. Ошибки глотаем: название — украшение, не данные пайплайна."""
        url = run.url
        if not url:
            return
        cached = self._title_cache.get(url)
        if cached:
            run.video_title = cached
            return

        def work():
            try:
                title = get_title(url)
            except Exception:
                title = ""
            if title:
                self._title_cache[url] = title
                run.video_title = title
                self._emit_queue()
        threading.Thread(target=work, daemon=True).start()

    def _queue_position(self, run_id):
        with self._qlock:
            try:
                return list(self._queue).index(run_id) + 1
            except ValueError:
                return 0

    def _maybe_start_next(self):
        """Start the next queued run if nothing is active and the queue isn't
        paused. Returns the started _Run (or None)."""
        with self._qlock:
            if self._active_run_id is not None or self._queue_paused:
                return None
            run = None
            while self._queue:
                rid = self._queue[0]
                r = self._runs.get(rid)
                if r is None or r.status != "queued":
                    self._queue.popleft()
                    continue
                run = r
                self._queue.popleft()
                break
            if run is None:
                return None
            run.status = "running"
            run.step_text = "Запускаю…"
            self._active_run_id = run.run_id
            self._cancel_event = run.cancel_event
            self._active_runner = None
        self._start_run_thread(run)
        self._emit_queue()
        return run

    def _start_run_thread(self, run):
        def wrap():
            _run_ctx.run_id = run.run_id
            # Make sure the previous run's Chrome profile lock is released before
            # this run can touch the single Claude browser (start() hard-fails on
            # a stale lock). Cheap no-op when no lock files are present.
            self._wait_profile_unlocked()
            try:
                asyncio.run(run.fn(*run.args))
            except (scenarios_mod.ScenarioCancelled, VoiceCancelled):
                # Cancellation of translate/preset pipelines surfaces here
                # (scenarios handle it inside _pipeline_scenario).
                log.info("Pipeline run %s cancelled", run.run_id)
                self._emit("cancelled", {})
            except Exception as e:
                tb = traceback.format_exc()
                log.error("Pipeline thread crashed: %s\n%s", e, tb)
                self._emit("error", {
                    "message": str(e) or e.__class__.__name__,
                    "traceback": tb,
                    "log_file": LOG_FILE,
                })
            finally:
                self._on_run_thread_exit(run.run_id)
        t = threading.Thread(target=wrap, daemon=True)
        run.thread = t
        self._active_thread = t
        t.start()

    def _on_run_thread_exit(self, run_id):
        """Called from each run worker's finally. Clears the active slot and
        advances the queue per the rule: a crash/finish continues to the next
        run, a user cancel pauses the whole queue. Auto-retries transient
        failures and fires completion/error notifications."""
        retry = None
        notify_error = None
        with self._qlock:
            run = self._runs.get(run_id)
            if run is not None and run.status in ("running", "waiting_input"):
                # Thread died without a terminal event — treat as error.
                run.status = "error"
                run.step_text = "Ошибка"
                run.error = run.error or {"message": "Запуск завершился неожиданно"}
            if self._active_run_id == run_id:
                self._active_run_id = None
                self._cancel_event = None
                self._active_runner = None
                self._active_thread = None
            if run is not None and run.status == "cancelled":
                # Manual cancel stops the chain; the user resumes it explicitly.
                self._queue_paused = True
            elif (run is not None and run.status == "error"
                    and run.retries_left > 0 and self._is_transient_error(run.error)):
                # Transient failure (network / timeout) → auto-retry as a fresh
                # run with the same params. Replace the errored entry so it
                # doesn't pile up; only the FINAL failure notifies the user.
                clone = self._new_run(run.kind, run.title, run.subtitle, run.icon,
                                      list(run.steps), list(run.step_meta), run.fn, run.args)
                self._copy_run_identity(run, clone)
                clone.retries_left = run.retries_left - 1
                clone.attempt = run.attempt + 1
                self._runs[clone.run_id] = clone
                self._queue.append(clone.run_id)
                self._batch_ids.add(clone.run_id)
                self._runs.pop(run_id, None)
                retry = clone
            elif run is not None and run.status == "error":
                notify_error = (run.title, (run.error or {}).get("message") or "Ошибка")
        if retry is not None:
            log.info("Auto-retry '%s' → attempt %d (%d left)",
                     retry.title, retry.attempt, retry.retries_left)
        self._emit_queue()
        self._maybe_start_next()
        # Notifications (outside the lock; the queue state is now settled).
        with self._qlock:
            still_working = (self._active_run_id is not None or bool(self._queue))
            idle = (not still_working and not self._queue_paused)
            batch = list(self._batch_ids) if idle else None
            if idle:
                self._batch_ids = set()
        # A mid-batch failure pings immediately; if it was the LAST run, the
        # completion summary below already reports it — don't double-notify.
        if notify_error is not None and still_working:
            self._notify(f"Ошибка: {notify_error[0]}", notify_error[1], "error")
        if batch:
            self._notify_queue_done(batch)

    # Error-message fragments that mark a TRANSIENT failure worth auto-retrying
    # (network blips / timeouts). Real errors — bad URL, empty prompt, censored
    # voice, auth — won't match, so we never loop on a hopeless run.
    _TRANSIENT_MARKERS = (
        "timeout", "timed out", "не завершил", "истекло время",
        "connection", "connect", "disconnected", "reset by peer",
        "max retries", "temporarily", "getaddrinfo", "ssl",
        "502", "503", "504", "remotedisconnected", "eof occurred",
        "readtimeout", "connecttimeout", "clientconnector", "serverdisconnected",
    )

    def _is_transient_error(self, error):
        if not error:
            return False
        blob = ((error.get("message") or "") + " " + (error.get("traceback") or "")).lower()
        return any(m in blob for m in self._TRANSIENT_MARKERS)

    def _notify(self, title: str, message: str, kind: str = "done"):
        """Audible + (optional) OS toast + in-app banner when a batch finishes
        or a run fails. Best-effort: any channel can fail silently."""
        try:
            if not settings.load().get("notify_on_complete", True):
                return
        except Exception:
            pass
        try:
            if sys.platform == "darwin":
                # macOS: winsound нет — играем системный звук через afplay.
                import subprocess
                snd = "Sosumi" if kind == "error" else "Glass"
                subprocess.Popen(["afplay", f"/System/Library/Sounds/{snd}.aiff"])
            else:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONHAND if kind == "error"
                                     else winsound.MB_ICONASTERISK)
        except Exception:
            pass
        try:
            from winotify import Notification  # optional dependency
            Notification(app_id="vizo", title=title, msg=message).show()
        except Exception:
            pass
        self._emit("notify", {"title": title, "message": message, "kind": kind})

    def _notify_queue_done(self, batch_ids):
        done = err = canc = 0
        last_done_title = None
        for rid in batch_ids:
            r = self._runs.get(rid)
            if r is None:
                continue
            if r.status == "done":
                done += 1
                last_done_title = r.title
            elif r.status == "error":
                err += 1
            elif r.status == "cancelled":
                canc += 1
        total = done + err + canc
        if total == 0:
            return
        if total == 1 and done == 1:
            self._notify("Готово", last_done_title or "Запуск завершён", "done")
            return
        parts = [f"{done} готово"]
        if err:
            parts.append(f"{err} с ошибкой")
        if canc:
            parts.append(f"{canc} отменено")
        self._notify("Очередь завершена", ", ".join(parts), "error" if err else "done")

    def _wait_profile_unlocked(self, timeout: float = 8.0):
        # Оба профиля (Claude и ChatGPT): предыдущий запуск мог держать
        # любой из них. Несуществующий/свободный профиль проходит мгновенно.
        from config import CHROME_PROFILE, GPT_CHROME_PROFILE
        names = ("SingletonLock", "lockfile", "SingletonCookie")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(os.path.exists(os.path.join(profile, n))
                       for profile in (CHROME_PROFILE, GPT_CHROME_PROFILE)
                       for n in names):
                return
            time.sleep(0.25)
        log.warning("Chrome profile still locked after %.0fs — starting anyway", timeout)

    def cancel(self, run_id: int = None):
        """Cancel a run. A queued (not-yet-started) run is simply removed and
        the queue keeps going. Cancelling the ACTIVE run stops it and pauses the
        whole queue (the user resumes explicitly)."""
        with self._qlock:
            rid = run_id if run_id is not None else self._active_run_id
            run = self._runs.get(rid) if rid is not None else None
            if run is None:
                self._emit("cancelled")
                return {"ok": True}
            if run.status == "queued":
                try:
                    self._queue.remove(rid)
                except ValueError:
                    pass
                run.status = "cancelled"
                run.step_text = "Отменён до запуска"
                run.finished_at = datetime.now().isoformat(timespec="seconds")
                self._save_history()
                self._emit_queue()
                return {"ok": True, "run_id": rid, "queued": True}
            is_active = (rid == self._active_run_id)
        if not is_active:
            return {"ok": False, "error": "Этот запуск уже не активен"}
        log.info("Cancelling active run %s", rid)
        self._signal_cancel()
        with self._qlock:
            if run.status in ("running", "waiting_input"):
                run.step_text = "Отмена…"
                run.pending_input = None
        self._emit_queue()
        return {"ok": True, "run_id": rid}

    def resume_queue(self):
        """Un-pause the queue (after a cancel) and start the next run."""
        with self._qlock:
            self._queue_paused = False
        self._maybe_start_next()
        self._emit_queue()
        return {"ok": True}

    def remove_run(self, run_id: int):
        """Remove a queued run from the queue, or drop a finished run from the
        history list. Refuses to touch the active run (cancel it first)."""
        with self._qlock:
            run = self._runs.get(run_id)
            if run is None:
                return {"ok": False, "error": "Запуск не найден"}
            if run.status == "queued":
                try:
                    self._queue.remove(run_id)
                except ValueError:
                    pass
                run.status = "cancelled"
                run.step_text = "Убран из очереди"
                run.finished_at = datetime.now().isoformat(timespec="seconds")
            elif run.status in ("done", "error", "cancelled"):
                self._runs.pop(run_id, None)
            else:
                return {"ok": False, "error": "Сначала отмени активный запуск"}
        self._save_history()
        self._emit_queue()
        return {"ok": True}

    def restart_run(self, run_id: int):
        """Re-queue a finished/cancelled run with the SAME parameters (incl. the
        URL / starting vars it was given), as a fresh run. Lets the user resume a
        cancelled run or retry a failed one without re-entering anything.
        The old finished entry is dropped so it doesn't linger as a duplicate."""
        with self._qlock:
            old = self._runs.get(run_id)
            if old is None:
                return {"ok": False, "error": "Запуск не найден"}
            if old.fn is None or old.args is None:
                # Восстановлен из истории без параметров (args были слишком
                # большие или несериализуемые) — перезапустить нечем.
                return {"ok": False, "error": "Для этого запуска из истории не "
                        "сохранились параметры — запусти его заново вручную"}
            run = self._new_run(old.kind, old.title, old.subtitle, old.icon,
                                list(old.steps), list(old.step_meta), old.fn, old.args)
            self._copy_run_identity(old, run)
            # Resuming replaces the old entry — remove it if it has finished.
            if old.status in ("done", "error", "cancelled"):
                self._runs.pop(run_id, None)
        self._save_history()
        return self._enqueue(run)

    def resume_run(self, run_id: int):
        """«Продолжить с места»: как restart_run, но в ТУ ЖЕ папку результата —
        пайплайн подхватит чекпоинт/готовые артефакты и пропустит выполненные
        шаги (Claude, озвучку, картинки не переделывает)."""
        with self._qlock:
            old = self._runs.get(run_id)
            if old is None:
                return {"ok": False, "error": "Запуск не найден"}
            if not old.is_resumable():
                return {"ok": False, "error": "Продолжить нечем — чекпоинт не "
                        "найден. Используй обычный перезапуск."}
            run = self._new_run(old.kind, old.title, old.subtitle, old.icon,
                                list(old.steps), list(old.step_meta), old.fn, old.args)
            self._copy_run_identity(old, run)
            run.output_dir = old.output_dir
            run.resume_requested = True
            self._runs.pop(run_id, None)
        self._save_history()
        return self._enqueue(run)

    def reorder_queue(self, ordered_ids: list):
        """Reorder the waiting queue. ordered_ids lists queued run ids in the
        desired order; any omitted queued runs keep their relative order at the end."""
        with self._qlock:
            wanted = [int(i) for i in (ordered_ids or [])]
            present = [rid for rid in wanted if rid in self._queue]
            remaining = [rid for rid in self._queue if rid not in present]
            self._queue = collections.deque(present + remaining)
        self._emit_queue()
        return {"ok": True}

    def list_runs(self):
        return self._queue_snapshot()

    def get_run_state(self, run_id: int):
        run = self._runs.get(run_id)
        if not run:
            return {"ok": False, "error": "Запуск не найден"}
        return run.public()

    def _queue_snapshot(self):
        with self._qlock:
            runs = [self._runs[rid].public() for rid in list(self._runs)]
            return {
                "runs": runs,
                "active_run_id": self._active_run_id,
                "queue_order": list(self._queue),
                "paused": self._queue_paused,
            }

    def _emit_queue(self):
        if not self._window:
            return
        import json as _json
        snap = self._queue_snapshot()
        self._window.evaluate_js(
            f"window.onPyEvent && window.onPyEvent('queue', {_json.dumps(snap)})"
        )

    def _signal_cancel(self):
        """Set the cancel flag and ask the active runner to abort, to unblock
        async waits inside the runner / Claude browser."""
        if self._cancel_event:
            self._cancel_event.set()
        runner = self._active_runner
        if runner is not None:
            runner.request_cancel()

    def skip_step(self, step_idx: int):
        """Пропустить шаг активного сценария (кнопка «Пропустить» в UI).
        Поддерживается длинными шагами: озвучка, видео, banana-картинки.
        Шаг завершается без результата, сценарий продолжается дальше."""
        runner = self._active_runner
        if runner is None:
            return {"ok": False, "error": "Нет активного запуска сценария"}
        try:
            idx = int(step_idx)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Некорректный номер шага"}
        runner.request_skip(idx)
        log.info("User requested skip of step %d", idx)
        return {"ok": True, "step_idx": idx}

    def cancel_all_veo_tasks(self):
        """Emergency: tell VeoNonStop to cancel every active video task on
        the account. Useful if the app crashed mid-generation or the user
        just wants to nuke everything."""
        try:
            data = veo_api.cancel_all()
            count = None
            if isinstance(data, dict):
                # По докам поле называется cancelled_count; старые имена
                # оставлены как фолбэк.
                count = (data.get("cancelled_count")
                         or data.get("cancelled") or data.get("count"))
            log.info("cancel_all_veo_tasks: %s", data)
            return {"ok": True, "cancelled": count, "raw": data}
        except Exception as e:
            log.exception("cancel_all_veo_tasks failed")
            return {"ok": False, "error": str(e)}

    def open_log_file(self):
        """Reveal the rotating log file in Explorer / Finder."""
        try:
            if os.name == "nt":
                os.startfile(os.path.dirname(LOG_FILE))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", os.path.dirname(LOG_FILE)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", os.path.dirname(LOG_FILE)])
            return {"ok": True, "path": LOG_FILE}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_folder(self, path: str):
        """Reveal a run's output folder in Explorer / Finder."""
        try:
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "Папка не найдена"}
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def read_log_tail(self, lines: int = 200):
        """Return the last N lines of the log file (for in-app viewing)."""
        try:
            if not os.path.exists(LOG_FILE):
                return {"ok": True, "lines": [], "path": LOG_FILE}
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                buf = f.readlines()
            return {"ok": True, "lines": buf[-lines:], "path": LOG_FILE}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- Internal pipeline helpers ---

    def _create_output_dir(self, label: str, suffix: str = "") -> str:
        rid = getattr(_run_ctx, "run_id", None) or self._active_run_id
        run = self._runs.get(rid) if rid is not None else None
        # «Продолжить с места» кладёт результаты в ТУ ЖЕ папку — чекпоинт и
        # готовые артефакты (script.txt, images/) лежат именно там.
        if run is not None and run.output_dir:
            os.makedirs(run.output_dir, exist_ok=True)
            return run.output_dir
        base = settings.load().get("output_dir") or settings.DEFAULT_OUTPUT_DIR
        # Имя, заданное пользователем при запуске, используется КАК ЕСТЬ, без
        # префикса даты: он назвал папку сам, чтобы легко её найти. Дата
        # добавляется только к автоименам.
        custom = _safe_name(run.folder_name + suffix) if (
            run is not None and run.folder_name) else ""
        if custom:
            folder = os.path.join(base, custom)
        else:
            date = datetime.now().strftime("%Y-%m-%d_%H-%M")
            folder = os.path.join(base, f"{date}_{_safe_name(label + suffix)}")
        # Уникализируем: «отменил и сразу перезапустил» в ту же минуту не
        # должно смешивать файлы двух запусков в одной папке.
        if os.path.exists(folder):
            for n in range(2, 100):
                candidate = f"{folder}_{n}"
                if not os.path.exists(candidate):
                    folder = candidate
                    break
        os.makedirs(folder, exist_ok=True)
        # Remember the folder on the run so an error report lands next to the
        # files, and the queue/status UI can offer "open folder".
        if run is not None and not run.output_dir:
            run.output_dir = folder
        return folder

    async def _wait_for_user_reply(self, message: str, kind: str = "claude",
                                   title: str = None, cancel_event=None) -> str:
        """Show a prompt to the user and wait for their reply via UI.
        kind='claude' → dialog screen with chat bubble, брендинг Claude
        kind='gpt' → тот же экран, брендинг ChatGPT
        kind='input' → generic input request from the scenario engine"""
        # Событие отмены захватываем на старте: self._cancel_event подменяется
        # каждым новым запуском, и старый (отменённый) пайплайн иначе опрашивал
        # бы ЧУЖОЙ event — и висел в этом цикле вечно.
        ev = cancel_event if cancel_event is not None else self._cancel_event
        self._pending_reply = None
        self._emit("user_input_request", {
            "message": message, "kind": kind, "title": title,
        })
        # Keep legacy event for Claude path so older UI still works
        if kind == "claude":
            self._emit("claude_asks", {"message": message})
        while self._pending_reply is None:
            if ev and ev.is_set():
                raise scenarios_mod.ScenarioCancelled()
            await asyncio.sleep(0.2)
        return self._pending_reply

    # --- SCENARIO pipeline ---

    async def _pipeline_scenario(self, scenario: dict, starting_vars: dict):
        output_dir = self._create_output_dir(scenario.get("name") or "scenario")
        steps_total = len(scenario.get("steps", []))
        cancel_event = self._cancel_event  # захватываем СВОЙ event (см. _wait_for_user_reply)

        # «Продолжить с места»: чекпоинт лежит в переиспользованной папке.
        # Нет/битый/от другой версии сценария → честно едем с нуля (в ту же папку).
        resume_state = None
        rid = getattr(_run_ctx, "run_id", None)
        cur_run = self._runs.get(rid) if rid is not None else None
        if cur_run is not None and cur_run.resume_requested:
            resume_state = scenarios_mod.load_checkpoint(output_dir, scenario)
            if resume_state is None:
                log.warning("Resume запрошен, но чекпоинт не подошёл — старт с нуля")

        def on_progress(idx, total, label, detail=None):
            pct = int(idx / max(total, 1) * 100) if total else 0
            self._progress(label or "...", pct, idx)
            # Always emit detail (even empty) so the UI clears the previous
            # step's substatus when a new step starts without one.
            self._emit("progress_detail", {"detail": detail or ""})

        async def on_ask_user(message: str, provider: str = "claude") -> str:
            # provider ("claude" | "gpt") → kind: экран ответа в UI показывает
            # бренд того чата, который спрашивает.
            return await self._wait_for_user_reply(message, kind=provider,
                                                   cancel_event=cancel_event)

        async def on_user_input(prompt: str) -> str:
            return await self._wait_for_user_reply(prompt, kind="input",
                                                   cancel_event=cancel_event)

        runner = scenarios_mod.ScenarioRunner(
            scenario,
            output_dir,
            on_progress=on_progress,
            on_ask_user=on_ask_user,
            on_user_input=on_user_input,
            starting_vars=starting_vars,
            cancel_event=cancel_event,
            resume_state=resume_state,
        )
        self._active_runner = runner
        try:
            self._emit("scenario_start", {
                "scenario_id": scenario.get("id"),
                "name": scenario.get("name"),
                "steps": [
                    {"idx": i, "name": s.get("name") or s.get("type"), "type": s.get("type")}
                    for i, s in enumerate(scenario.get("steps", []))
                ],
            })
            await runner.run()
            # Record last_run WITHOUT clobbering edits the user may have made in
            # the editor while the run was in flight: persist a minimal patch
            # (id + last_run_at). save() merges it over the freshly-loaded record.
            try:
                sid = scenario.get("id")
                if sid:
                    scenarios_mod.save({"id": sid, "last_run_at": datetime.now().isoformat()})
            except Exception:
                pass
            self._emit("done", {"output_dir": output_dir})
        except scenarios_mod.ScenarioCancelled:
            log.info("Scenario '%s' cancelled at step %s/%s", scenario.get("name"),
                     getattr(runner, "_current_idx", "?"), steps_total)
            self._emit("cancelled", {
                "output_dir": output_dir,
                "cancelled_at_step": getattr(runner, "_current_idx", None),
            })
        except Exception as e:
            tb = traceback.format_exc()
            log.error(
                "Scenario '%s' failed at step %s/%s: %s\n%s",
                scenario.get("name"),
                getattr(runner, "_current_idx", "?"),
                steps_total,
                e,
                tb,
            )
            self._emit("error", {
                "message": str(e) or e.__class__.__name__,
                "traceback": tb,
                "log_file": LOG_FILE,
                "output_dir": output_dir,
                "failed_step_idx": getattr(runner, "_current_idx", None),
            })

    # --- TRANSLATE pipeline ---

    async def _pipeline_translate(self, url: str, language: str):
        # Try to resolve language → voice via user_languages first.
        # `language` from UI may be either the language name ("Венгерский")
        # or a language id (e.g. "hu"). Fall back to legacy LANG_TO_PRESET.
        preset_key = None
        try:
            for l in languages_mod.load_all():
                if l.get("name") == language or l.get("id") == language:
                    preset_key = l.get("voice_id")
                    break
        except Exception:
            pass
        if not preset_key:
            try:
                preset_key = resolve_lang(language)
            except KeyError as e:
                self._emit("error", {"message": str(e)})
                return

        # Отмена: раньше Cancel в этом пайплайне не проверялся вообще —
        # поток продолжал крутить Claude/озвучку и жечь деньги после отмены.
        cancel_event = self._cancel_event
        is_cancelled = (lambda: bool(cancel_event and cancel_event.is_set()))

        def check_cancel():
            if is_cancelled():
                raise scenarios_mod.ScenarioCancelled()

        # «Продолжить с места»: если в переиспользованной папке уже лежит
        # script.txt — самая дорогая часть (Claude) готова, начинаем с превью
        # и озвучки. _create_output_dir у resume-запуска возвращает СТАРУЮ папку.
        rid = getattr(_run_ctx, "run_id", None)
        cur_run = self._runs.get(rid) if rid is not None else None
        resuming = bool(cur_run is not None and cur_run.resume_requested
                        and cur_run.output_dir)
        translated = None
        output_dir = None
        if resuming:
            output_dir = self._create_output_dir(f"translate_{language}")
            try:
                sp = os.path.join(output_dir, "script.txt")
                if os.path.getsize(sp) > 0:
                    with open(sp, encoding="utf-8") as f:
                        translated = f.read()
                    log.info("translate resume: script.txt найден (%d симв.) — "
                             "Claude пропускаем", len(translated))
            except OSError:
                pass

        if translated is None:
            self._progress("Извлекаю транскрипт...", 5, 0)
            transcript = get_transcript(url)
            check_cancel()

            self._progress("Получаю заголовок и описание...", 12, 1)
            orig_title = get_title(url)
            orig_description = get_description(url)
            check_cancel()

            if output_dir is None:
                output_dir = self._create_output_dir(f"translate_{language}")

            self._progress("Запускаю Claude...", 18, 2)
            claude = ClaudeAutomation()
            try:
                await claude.start(status_cb=lambda m: self._progress(m, 18, 2))
                check_cancel()
                self._progress(f"Перевожу на {language}...", 30, 3)
                translated = await claude.run_translate(transcript, language,
                                                        is_cancelled=is_cancelled)

                self._progress("Генерирую SEO...", 65, 4)
                seo_title, seo_description = await claude.run_seo(
                    orig_title, orig_description, language, is_cancelled=is_cancelled)
            finally:
                await claude.close()
            check_cancel()

            with open(os.path.join(output_dir, "script.txt"), "w", encoding="utf-8") as f:
                f.write(translated)
            with open(os.path.join(output_dir, "seo.txt"), "w", encoding="utf-8") as f:
                f.write(f"{seo_title}\n\n\n\n{seo_description}")
        else:
            self._progress("Сценарий уже переведён — продолжаю", 75, 4)

        thumbnail_path = os.path.join(output_dir, "thumbnail.jpg")
        voice_path     = os.path.join(output_dir, "voiceover.mp3")

        if not (resuming and os.path.exists(thumbnail_path)):
            self._progress("Скачиваю превью...", 80, 5)
            download_thumbnail(url, thumbnail_path)

        # Готовую озвучку из прошлой попытки не переозвучиваем — это деньги.
        voice_ready = False
        if resuming:
            try:
                voice_ready = os.path.getsize(voice_path) > 0
            except OSError:
                voice_ready = False
        if not voice_ready:
            self._progress("Озвучиваю...", 90, 6)
            loop = asyncio.get_event_loop()
            voice_status = lambda msg: self._emit("progress_detail", {"detail": msg})
            await loop.run_in_executor(
                None, lambda: synthesize(translated, preset_key, voice_path,
                                         cancel_check=is_cancelled,
                                         status_callback=voice_status)
            )

        self._progress("Готово!", 100, 6)  # last valid step index (UI list has 7: 0..6)
        self._emit("done", {"output_dir": output_dir})

    # --- PRESET pipeline (tartaria) ---

    async def _pipeline_preset(self, url: str, preset: str, generate_images_flag: bool = True):
        if preset != "tartaria":
            self._emit("error", {"message": f"Пресет '{preset}' пока не поддерживается"})
            return

        cancel_event = self._cancel_event
        is_cancelled = (lambda: bool(cancel_event and cancel_event.is_set()))

        def check_cancel():
            if is_cancelled():
                raise scenarios_mod.ScenarioCancelled()

        self._progress("Получаю заголовок видео...", 3, 0)
        title = get_title(url)
        if not title:
            self._emit("error", {"message": "Не удалось получить заголовок видео"})
            return

        self._progress("Извлекаю транскрипт...", 8, 1)
        transcript = get_transcript(url)
        check_cancel()

        output_dir = self._create_output_dir(title)

        self._progress("Запускаю Claude...", 12, 2)
        claude = ClaudeAutomation()
        try:
            await claude.start(status_cb=lambda m: self._progress(m, 12, 2))
            check_cancel()

            # Custom tartaria flow with UI-based reply
            await claude._run_tartaria_with_ui(title, transcript, self,
                                               is_cancelled=is_cancelled)
            script = claude._last_script
            image_prompts = claude._last_image_prompts
        finally:
            await claude.close()
        check_cancel()

        script_path  = os.path.join(output_dir, "script.txt")
        prompts_path = os.path.join(output_dir, "image_prompts.txt")
        images_dir   = os.path.join(output_dir, "images")
        voice_path   = os.path.join(output_dir, "voiceover.mp3")

        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        with open(prompts_path, "w", encoding="utf-8") as f:
            f.write(image_prompts)

        prompts_list = [p.strip() for p in image_prompts.splitlines() if p.strip()]

        loop = asyncio.get_event_loop()
        voice_status = lambda msg: self._emit("progress_detail", {"detail": msg})
        synth = lambda: synthesize(script, "tartaria", voice_path,
                                   cancel_check=is_cancelled,
                                   status_callback=voice_status)
        if generate_images_flag:
            self._progress("Озвучка + картинки параллельно...", 60, 7)
            await asyncio.gather(
                loop.run_in_executor(None, synth),
                generate_images(prompts_list, images_dir),
            )
        else:
            self._progress("Озвучка...", 80, 7)
            await loop.run_in_executor(None, synth)

        self._progress("Готово!", 100, 8 if generate_images_flag else 7)
        self._emit("done", {"output_dir": output_dir})

    # --- GENERATION pipeline (standalone VeoNonStop) ---

    async def _pipeline_generation(self, mode: str, params: dict):
        out_dir = self._create_output_dir(f"gen_{mode}")
        loop = asyncio.get_event_loop()
        try:
            if mode == "banana_image":
                await self._gen_banana_image(out_dir, params)
            elif mode == "text_to_video":
                await self._gen_video(out_dir, params, "text_to_video", loop)
            elif mode == "image_to_video":
                await self._gen_video(out_dir, params, "image_to_video", loop)
            elif mode == "multi_image":
                await self._gen_video(out_dir, params, "multi_image", loop)
            elif mode == "batch_frame":
                await self._gen_video(out_dir, params, "batch_frame", loop)
            else:
                self._emit("error", {"message": f"Неизвестный режим: {mode}"})
                return
            self._emit("done", {"output_dir": out_dir})
        except Exception as e:
            self._emit("error", {"message": str(e)})

    async def _gen_banana_image(self, out_dir: str, params: dict):
        prompt = params.get("prompt", "").strip()
        if not prompt:
            raise RuntimeError("Промпт пуст")
        num_images = int(params.get("num_images", 1))
        model_key = params.get("model_key") or "GEM_PIX_2"
        aspect_ratio = params.get("aspect_ratio") or "16:9"

        self._progress("Генерирую изображения...", 30, 0)
        data = await veo_api.banana_generate_with_retry(
            prompt=prompt,
            num_images=num_images,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
        )
        media = data.get("media") or []
        if not media:
            raise RuntimeError("API вернул пустой media[]")

        self._progress("Скачиваю...", 80, 1)
        os.makedirs(out_dir, exist_ok=True)
        import aiohttp
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(media, 1):
                fife_url = item.get("fifeUrl") or item.get("url")
                if not fife_url:
                    continue
                out_path = os.path.join(out_dir, f"image_{i:03d}.png")
                async with session.get(fife_url) as r:
                    r.raise_for_status()
                    content = await r.read()
                with open(out_path, "wb") as f:
                    f.write(content)
        self._progress("Готово!", 100, 2)

    async def _gen_video(self, out_dir: str, params: dict, mode: str, loop):
        prompt = params.get("prompt", "").strip()
        if not prompt:
            raise RuntimeError("Промпт пуст")
        aspect_ratio = params.get("aspect_ratio") or "16:9"
        count = int(params.get("count", 1))
        duration = params.get("duration") or None

        self._progress("Создаю задачу видео...", 5, 0)

        if mode == "text_to_video":
            task_id = await loop.run_in_executor(
                None, veo_api.text_to_video, prompt, aspect_ratio, count, duration,
            )
        elif mode == "image_to_video":
            image_path = params.get("image_path")
            if not image_path or not os.path.exists(image_path):
                raise RuntimeError("Картинка не указана")
            b64, mime = veo_api.file_to_base64(image_path)
            task_id = await loop.run_in_executor(
                None, veo_api.image_to_video,
                prompt, b64, mime, aspect_ratio, count, duration,
            )
        elif mode == "multi_image":
            paths = params.get("image_paths") or []
            if len(paths) < 1:
                raise RuntimeError("Загрузи хотя бы одну картинку")
            images = []
            for p in paths:
                if not os.path.exists(p):
                    raise RuntimeError(f"Файл не найден: {p}")
                b64, mime = veo_api.file_to_base64(p)
                # имя должно быть только латиницей
                name = "".join(c for c in os.path.splitext(os.path.basename(p))[0] if c.isascii() and c.isalpha())
                if not name:
                    name = f"img{len(images) + 1}"
                images.append({"name": name, "image_base64": b64, "mime_type": mime})
            task_id = await loop.run_in_executor(
                None, veo_api.multi_image_to_video,
                prompt, images, aspect_ratio, count,
            )
        elif mode == "batch_frame":
            start = params.get("start_image_path")
            end = params.get("end_image_path")
            if not start or not end:
                raise RuntimeError("Нужны стартовая и конечная картинки")
            b64s, _ = veo_api.file_to_base64(start)
            b64e, _ = veo_api.file_to_base64(end)
            task_id = await loop.run_in_executor(
                None, veo_api.batch_frame,
                prompt, b64s, b64e, aspect_ratio, count,
            )
        else:
            raise RuntimeError(f"Неизвестный режим видео: {mode}")

        self._emit("video_task", {"task_id": task_id, "mode": mode})

        def on_progress(data: dict):
            status = str(data.get("status", "")).upper()
            prog = data.get("progress") or {}
            done = prog.get("completed") or 0
            total = prog.get("total") or count or 1
            pct = int(min(95, 10 + int(done / max(total, 1) * 85)))
            self._progress(f"{status} {done}/{total}", pct, 1)

        result = await loop.run_in_executor(None, veo_api.wait_for_video, task_id, on_progress)

        videos = result.get("videos") or []
        self._progress("Скачиваю видео...", 95, 2)
        os.makedirs(out_dir, exist_ok=True)
        for i in range(len(videos)):
            out_path = os.path.join(out_dir, f"video_{i + 1:02d}.mp4")
            await loop.run_in_executor(None, veo_api.download_video, task_id, out_path, i)

        self._progress("Готово!", 100, 3)
