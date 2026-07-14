"""User-defined scenario pipelines.

A scenario is a JSON document like:
{
  "id": "s_xxx",
  "name": "Перевод на польский с озвучкой",
  "icon": "🌐",
  "description": "...",
  "created_at": "...",
  "last_run_at": "...",
  "steps": [
    { "id": "st1", "type": "yt_url", "name": "URL", "output": "url" },
    { "id": "st2", "type": "yt_transcript", "name": "Транскрипт",
      "output": "transcript" },
    { "id": "st3", "type": "claude_open", "name": "Открыть чат Claude" },
    { "id": "st4", "type": "claude_prompt", "name": "Перевод",
      "prompt_template": "Переведи на польский:\\n{transcript}",
      "output": "translated" },
    { "id": "st5", "type": "save_txt", "name": "script.txt",
      "filename": "script.txt", "source": "translated" },
    { "id": "st6", "type": "voice", "name": "Озвучка",
      "preset": "pl", "source": "translated",
      "filename": "voiceover.mp3", "output": "voice_mp3" }
  ]
}

Storage: `user_settings/user_scenarios.json` (a list of scenarios).
"""
import asyncio
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from config import USER_DATA_DIR, PROMPTS_DIR
from modules.logger import get as get_logger

log = get_logger("vizo.scenarios")

SCENARIOS_FILE = os.path.join(USER_DATA_DIR, "user_scenarios.json")


def _build_tartaria_scenario():
    """Read prompts from disk and assemble the Tartaria scenario.
    Returns None if prompt files are missing."""
    adapt_path = os.path.join(PROMPTS_DIR, "tartaria", "adaptation.txt")
    pics_path = os.path.join(PROMPTS_DIR, "tartaria", "pictures.txt")
    examples_dir = os.path.join(PROMPTS_DIR, "tartaria", "examples")
    if not (os.path.exists(adapt_path) and os.path.exists(pics_path)):
        return None
    with open(adapt_path, encoding="utf-8") as f:
        adapt = f.read()
    with open(pics_path, encoding="utf-8") as f:
        pics = f.read()
    examples = []
    if os.path.isdir(examples_dir):
        examples = sorted(
            os.path.join(examples_dir, n)
            for n in os.listdir(examples_dir)
            if n.lower().endswith(".png")
        )
    # Add {title} placeholder + {transcript} at the end
    adapt_tpl = adapt.replace(
        "Тема историй (оригинал):",
        "Тема историй (оригинал): {title}",
        1,
    )
    full_prompt = adapt_tpl.rstrip() + "\n\n---\n\n{transcript}"
    return {
        "id": "builtin_tartaria",
        "icon": "🏛",
        "name": "Тартария",
        "description": "Адаптация англ. видео на русский с расширением до 35 000 символов + ~100 промптов картинок + озвучка + Banana картинки параллельно.",
        "builtin": True,
        "steps": [
            {"id": "t1", "type": "yt_url", "name": "URL", "output": "url"},
            {"id": "t2", "type": "yt_title", "name": "Заголовок видео",
             "inputs": {"url": "{url}"}, "output": "title"},
            {"id": "t3", "type": "yt_transcript", "name": "Транскрипт",
             "inputs": {"url": "{url}"}, "output": "transcript"},
            {"id": "t4", "type": "claude_open", "name": "Открыть чат Claude",
             "model": "Opus 4.8", "effort": "max"},
            {"id": "t5", "type": "claude_prompt", "name": "Промпт адаптации",
             "prompt_template": full_prompt, "timeout": 300, "output": "adapted"},
            {"id": "t6", "type": "claude_ask", "name": "Ответ пользователя",
             "show_var": "adapted", "timeout": 1200, "output": "script"},
            {"id": "t7", "type": "claude_prompt", "name": "Промпты для картинок",
             "prompt_template": pics, "file_paths": examples, "timeout": 600,
             "output": "image_prompts"},
            {"id": "t8", "type": "save_txt", "name": "Сохранить script.txt",
             "filename": "script.txt", "source": "script"},
            {"id": "t9", "type": "save_txt", "name": "Сохранить image_prompts.txt",
             "filename": "image_prompts.txt", "source": "image_prompts"},
            {"id": "t10", "type": "voice", "name": "Озвучка",
             "preset": "tartaria", "source": "script",
             "filename": "voiceover.mp3", "output": "voice_mp3"},
            {"id": "t11", "type": "banana_batch", "name": "Картинки по промптам",
             "source": "image_prompts", "model": "GEM_PIX_2", "aspect_ratio": "16:9",
             "output": "images", "parallel_with_previous": True},
        ],
    }

# ── Built-in starter scenarios (seeded on first run, user-editable after) ──

BUILTIN_SCENARIOS_STATIC = [
    {
        "id": "builtin_translate_pl",
        "icon": "🇵🇱",
        "name": "Перевод на польский",
        "description": "URL → транскрипт → Claude перевод → озвучка",
        "steps": [
            {"id": "s1", "type": "yt_url",        "name": "URL",        "output": "url"},
            {"id": "s2", "type": "yt_transcript", "name": "Транскрипт", "output": "transcript",
             "inputs": {"url": "{url}"}},
            {"id": "s3", "type": "claude_open",   "name": "Открыть чат Claude"},
            {"id": "s4", "type": "claude_prompt", "name": "Перевод",   "output": "translated",
             "prompt_template": "Переведи это видео на польский язык, сохраняя разговорный стиль:\n\n{transcript}"},
            {"id": "s5", "type": "save_txt",      "name": "script.txt",
             "filename": "script.txt", "source": "translated"},
            {"id": "s6", "type": "voice",         "name": "Озвучка",
             "preset": "pl", "source": "translated", "filename": "voiceover.mp3",
             "output": "voice_mp3"},
        ],
    },
    {
        "id": "builtin_voice_only",
        "icon": "🎙",
        "name": "Только озвучка",
        "description": "Берёт текст и озвучивает выбранным голосом",
        "steps": [
            {"id": "s1", "type": "user_text", "name": "Текст для озвучки", "output": "text"},
            {"id": "s2", "type": "voice",     "name": "Озвучка",
             "preset": "tartaria", "source": "text", "filename": "voice.mp3",
             "output": "voice_mp3"},
        ],
    },
]


def _all_builtins() -> list:
    out = []
    tartaria = _build_tartaria_scenario()
    if tartaria:
        out.append(tartaria)
    out.extend(copy.deepcopy(BUILTIN_SCENARIOS_STATIC))
    return out


# «Файл есть, но не читается» ≠ «файла нет»: повреждённый user_scenarios.json
# (десятки КБ пользовательских пайплайнов) нельзя молча затирать встроенными.
_CORRUPT = object()


def _read_file():
    if not os.path.exists(SCENARIOS_FILE):
        return None
    try:
        with open(SCENARIOS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return _CORRUPT


def _write_file(scenarios: list) -> None:
    # Атомарная запись (как settings.py) — crash посреди записи не должен
    # оставить усечённый JSON, который при следующем старте сбросит сценарии.
    os.makedirs(os.path.dirname(SCENARIOS_FILE) or ".", exist_ok=True)
    tmp = SCENARIOS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(scenarios, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SCENARIOS_FILE)


def load_all() -> list:
    data = _read_file()
    if data is _CORRUPT:
        try:
            os.replace(SCENARIOS_FILE, SCENARIOS_FILE + ".bak")
            log.error("user_scenarios.json повреждён — отложен в user_scenarios.json.bak")
        except Exception:
            pass
        data = None
    if data is None:
        data = _all_builtins()
        _write_file(data)
    return data


def restore_defaults() -> int:
    """Re-add any built-in scenarios that the user has deleted. Custom
    scenarios and user-edited built-ins are preserved."""
    data = load_all()
    existing_ids = {s.get("id") for s in data}
    restored = 0
    for builtin in _all_builtins():
        if builtin["id"] not in existing_ids:
            data.append(builtin)
            restored += 1
    if restored:
        _write_file(data)
    return restored


def get(scenario_id: str) -> Optional[dict]:
    for s in load_all():
        if s.get("id") == scenario_id:
            return s
    return None


def save(scenario: dict) -> dict:
    scenarios = load_all()
    sid = scenario.get("id")
    if not sid:
        scenario["id"] = "s_" + uuid.uuid4().hex[:10]
        scenario.setdefault("created_at", datetime.now().isoformat())
        scenarios.append(scenario)
    else:
        for i, s in enumerate(scenarios):
            if s.get("id") == sid:
                scenarios[i] = {**s, **scenario}
                scenario = scenarios[i]
                break
        else:
            scenarios.append(scenario)
    _write_file(scenarios)
    return scenario


def delete(scenario_id: str) -> bool:
    scenarios = load_all()
    new_list = [s for s in scenarios if s.get("id") != scenario_id]
    if len(new_list) == len(scenarios):
        return False
    _write_file(new_list)
    return True


def duplicate(scenario_id: str) -> Optional[dict]:
    src = get(scenario_id)
    if not src:
        return None
    dup = copy.deepcopy(src)
    dup.pop("id", None)
    dup["name"] = (src.get("name") or "Сценарий") + " (копия)"
    return save(dup)


# ─────────────────────────────────────────────────────────────────────────
# VARIABLE SUBSTITUTION
# ─────────────────────────────────────────────────────────────────────────
VAR_RE = re.compile(r"\{(\w+)\}")


def substitute(text: str, vars_: dict) -> str:
    if not text:
        return text
    def replace(m):
        name = m.group(1)
        v = vars_.get(name)
        return str(v) if v is not None else m.group(0)
    return VAR_RE.sub(replace, text)


# ─────────────────────────────────────────────────────────────────────────
# CHUNKING (шаг claude_chunked: режем оригинал по концам предложений)
# ─────────────────────────────────────────────────────────────────────────
# Пресеты размера куска из UI → (нижняя, верхняя граница в символах).
CHUNK_SIZES = {
    "2-3k": (2000, 3000),
    "5-7k": (5000, 7000),
    "10-15k": (10000, 15000),
}
DEFAULT_CHUNK_SIZE = "5-7k"

# Конец предложения: . ? ! … (с учётом подряд идущих — «?!», «...»), затем
# закрывающие кавычки/скобки, пробелы и переводы строк — чтобы знак и хвост
# оставались с предложением, а не уезжали в следующий кусок.
_SENTENCE_RE = re.compile(r".*?[.?!…]+[\"»”’'`)\]]*[ \t]*(?:\r?\n)*", re.S)


def split_sentences(text: str) -> list:
    """Разбить текст на предложения по концам (. ? ! …). Хвост без концевого
    знака возвращается отдельным элементом, чтобы ничего не потерять."""
    if not text:
        return []
    sents = _SENTENCE_RE.findall(text)
    consumed = sum(len(s) for s in sents)
    if consumed < len(text):
        tail = text[consumed:]
        if tail:
            sents.append(tail)
    return sents


def chunk_text(text: str, lo: int, hi: int) -> list:
    """Сгруппировать предложения в куски примерно [lo, hi] символов, НИКОГДА
    не разрывая предложение. Кусок закрывается, как только дорос до нижней
    границы. Если одно предложение длиннее верхней границы (точки нет) — кусок
    будет длиннее, резать посреди предложения не будем."""
    sents = split_sentences(text)
    chunks = []
    cur = ""
    for s in sents:
        # Стараемся не переполнять верхнюю границу: если кусок непустой и
        # добавление предложения уводит за hi — закрываем сейчас (даже если ещё
        # не дорос до lo, иначе одно длинное предложение раздует кусок).
        if cur and len(cur) + len(s) > hi:
            chunks.append(cur)
            cur = ""
        cur += s
        if len(cur) >= lo:
            chunks.append(cur)
            cur = ""
    if cur.strip():
        chunks.append(cur)
    return chunks


# ─────────────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────────────
NODE_OUTPUT_TYPE = {
    "yt_url": "text", "user_text": "text", "file": "file",
    "yt_title": "text", "yt_desc": "text", "yt_transcript": "text",
    "yt_preview": "image",
    "claude_prompt": "text", "claude_ask": "text", "claude_chunked": "text",
    "gpt_prompt": "text", "gpt_ask": "text", "gpt_chunked": "text",
    "save_txt": "text", "save_json": "text",
    "voice": "audio",
    "banana_one": "image", "banana_batch": "image",
    "video_t2v": "video", "video_i2v": "video",
    "video_comp": "video", "video_batch": "video",
}

# AI-провайдеры чатов. Типы шагов строятся как "{провайдер}_{действие}"
# (claude_open, gpt_prompt, …) — сессии, валидация и resume-откат ведутся
# по каждому провайдеру НЕЗАВИСИМО: в смешанном сценарии Claude и ChatGPT
# открыты одновременно в двух отдельных окнах браузера.
AI_PROVIDERS = ("claude", "gpt")
AI_PROVIDER_LABEL = {"claude": "Claude", "gpt": "ChatGPT"}
# Действия, живущие в контексте открытого чата (требуют «{provider}_open»).
_AI_CHAT_ACTIONS = ("prompt", "ask", "chunked")


def ai_step_provider(step_type: str) -> str | None:
    """'gpt_prompt' → 'gpt'; None для шагов, не относящихся к AI-чатам."""
    if not step_type or "_" not in step_type:
        return None
    p = step_type.split("_", 1)[0]
    return p if p in AI_PROVIDERS else None


def validate(scenario: dict) -> list:
    """Return list of error dicts: { step_index, message }."""
    errors = []
    steps = scenario.get("steps", [])
    # Открытые AI-чаты считаем ПО провайдерам: gpt_prompt после claude_open
    # (без gpt_open) — ошибка, чаты не взаимозаменяемы.
    open_chats = {p: False for p in AI_PROVIDERS}
    declared_vars = set()

    for idx, step in enumerate(steps):
        t = step.get("type")
        # AI chat session validation (per provider)
        prov = ai_step_provider(t)
        if prov:
            action = t.split("_", 1)[1]
            if action == "open":
                open_chats[prov] = True
            elif action in _AI_CHAT_ACTIONS and not open_chats[prov]:
                errors.append({
                    "step_index": idx,
                    "message": "Сначала добавь шаг «Открыть новый чат "
                               f"{AI_PROVIDER_LABEL[prov]}»",
                })
            elif action == "close":
                open_chats[prov] = False

        # Validate brace references {var} in templated fields.
        for field in ("prompt_template", "filename", "source"):
            val = step.get(field)
            if isinstance(val, str):
                for ref in VAR_RE.findall(val):
                    if ref not in declared_vars and not _is_runtime_var(ref):
                        errors.append({
                            "step_index": idx,
                            "message": f"Переменная {{{ref}}} не определена в предыдущих шагах",
                        })

        # A bare `source` (no braces) is itself a variable name — verify it,
        # otherwise a typo like 'scrpit' silently passes and a voice/save step
        # writes the literal word instead of the intended text.
        src = step.get("source")
        if isinstance(src, str) and src and "{" not in src:
            if src not in declared_vars and not _is_runtime_var(src):
                errors.append({
                    "step_index": idx,
                    "message": f"Переменная '{src}' не определена в предыдущих шагах",
                })

        # Register this step's own output AFTER checking its references, so a
        # step that accidentally references its own output is flagged.
        if step.get("output"):
            declared_vars.add(step["output"])

        # Per-type required-field checks
        if t in ("claude_prompt", "gpt_prompt") and not step.get("prompt_template"):
            errors.append({
                "step_index": idx,
                "message": "Не указан шаблон промпта",
            })
        if t == "voice":
            if not step.get("preset"):
                errors.append({
                    "step_index": idx,
                    "message": "Не выбран голос",
                })
            if not step.get("source"):
                errors.append({
                    "step_index": idx,
                    "message": "Не указан источник текста (переменная)",
                })
        if t in ("claude_chunked", "gpt_chunked") and not step.get("source"):
            errors.append({
                "step_index": idx,
                "message": "Не указан источник — переменная с оригиналом для нарезки",
            })

    return errors


def _is_runtime_var(name: str) -> bool:
    # Variables generated at runtime that aren't from explicit step outputs
    return name in ("output_dir",)


# ─────────────────────────────────────────────────────────────────────────
# CHECKPOINTS (дозапуск с середины)
# ─────────────────────────────────────────────────────────────────────────
# После каждого выполненного батча раннер пишет в папку результата снимок
# переменных + сколько батчей готово. «Продолжить с места» пропускает готовые
# шаги — 20-минутная генерация в Claude и оплаченная озвучка не повторяются.
CHECKPOINT_FILENAME = ".vizo_state.json"


def steps_fingerprint(steps: list) -> str:
    """Отпечаток структуры шагов. Если сценарий отредактировали после падения,
    старый чекпоинт к нему уже не подходит — сравнение отпечатков это ловит."""
    import hashlib
    raw = json.dumps([(s.get("id"), s.get("type")) for s in (steps or [])])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_checkpoint(output_dir: str, scenario: dict) -> Optional[dict]:
    """Прочитать чекпоинт из папки результата. None — нет/битый/не от этого
    сценария (структура шагов изменилась)."""
    path = os.path.join(output_dir or "", CHECKPOINT_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return None
        if state.get("fingerprint") != steps_fingerprint(scenario.get("steps", [])):
            log.warning("Чекпоинт не подходит: сценарий изменился после падения")
            return None
        if not isinstance(state.get("completed_batches"), int):
            return None
        return state
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Не смог прочитать чекпоинт %s", path)
        return None


# ─────────────────────────────────────────────────────────────────────────
# EXECUTION ENGINE
# ─────────────────────────────────────────────────────────────────────────
class ScenarioCancelled(Exception):
    """Raised when the user cancels a running scenario from the UI."""
    pass


class StepSkipped(Exception):
    """Пользователь пропустил конкретный шаг из UI: шаг завершается без
    результата, сценарий продолжается со следующего шага."""
    pass


class ScenarioRunner:
    """Executes a scenario step-by-step. Calls `on_progress(idx, total,
    label, detail)` for each step. Calls `on_ask_user(message, provider)`
    (async, must return user reply) when a `claude_ask`/`gpt_ask` step runs —
    `provider` ("claude" | "gpt") задаёт брендинг диалога в UI.
    """

    def __init__(self, scenario: dict, output_dir: str,
                 on_progress: Optional[Callable] = None,
                 on_ask_user: Optional[Callable] = None,
                 on_user_input: Optional[Callable] = None,
                 starting_vars: Optional[dict] = None,
                 cancel_event=None,
                 resume_state: Optional[dict] = None):
        self.scenario = scenario
        self.output_dir = output_dir
        # Чекпоинт прошлого (упавшего/отменённого) запуска в этой же папке:
        # {"completed_batches": N, "vars": {...}} — см. load_checkpoint().
        self._resume_state = resume_state
        self.on_progress = on_progress or (lambda *a, **k: None)
        self.on_ask_user = on_ask_user
        self.on_user_input = on_user_input
        self.vars = dict(starting_vars or {})
        self.vars["output_dir"] = output_dir
        # Активные браузерные сессии AI-чатов по провайдерам:
        # ClaudeAutomation / GPTAutomation с одинаковым интерфейсом.
        self._ai_sessions: dict = {p: None for p in AI_PROVIDERS}
        self._current_idx = 0
        self._total_steps = 0
        # Name of the variable that received the most recent reply per
        # provider — used as default `show_var` for claude_ask/gpt_ask steps.
        self._last_reply_var: dict = {p: None for p in AI_PROVIDERS}
        # threading.Event set from the outside (api_bridge) when the user
        # presses Cancel. Checked between steps and during long Claude waits.
        self._cancel_event = cancel_event
        # VeoNonStop video task ids currently being polled. On cancel we send
        # /video/cancel/{task_id} for each so server-side compute stops.
        self._active_video_tasks: set[str] = set()
        # Индексы шагов, которые пользователь попросил пропустить. Длинные
        # ожидания (озвучка, видео, banana) опрашивают этот набор и поднимают
        # StepSkipped — шаг завершается без результата, сценарий идёт дальше.
        self._skip_steps: set[int] = set()

    def request_cancel(self):
        """Set the cancel flag. The runner's own async loops poll this between
        steps and inside long waits (Claude generation, voice polling)."""
        if self._cancel_event is not None:
            self._cancel_event.set()

    def request_skip(self, step_idx: int):
        """Пометить шаг как пропускаемый (вызывается из api_bridge по кнопке
        «Пропустить» в UI). Читается из других потоков — set + GIL достаточно."""
        self._skip_steps.add(int(step_idx))
        log.info("Skip requested for step %d", step_idx)

    def _skip_requested(self, step_idx: int) -> bool:
        return step_idx in self._skip_steps

    def _check_skip(self, step_idx: int):
        if self._skip_requested(step_idx):
            raise StepSkipped()

    def _cancelled(self) -> bool:
        return bool(self._cancel_event and self._cancel_event.is_set())

    def _check_cancel(self):
        if self._cancelled():
            raise ScenarioCancelled()

    async def run(self):
        steps = self.scenario.get("steps", [])
        total = len(steps)
        self._total_steps = total
        # Group into parallel batches. A step with parallel_with_previous=True
        # joins the previous batch; otherwise it starts a new batch.
        batches = []
        for i, step in enumerate(steps):
            if step.get("parallel_with_previous") and batches:
                batches[-1].append((i, step))
            else:
                batches.append([(i, step)])

        # Дозапуск с середины: пропустить батчи, выполненные до падения,
        # и восстановить накопленные переменные из чекпоинта.
        start_batch = 0
        if self._resume_state:
            start_batch = self._resume_start_batch(batches)
            for k, v in (self._resume_state.get("vars") or {}).items():
                self.vars.setdefault(k, v)   # свежие starting_vars важнее
            self.vars["output_dir"] = self.output_dir
            skipped = sum(len(b) for b in batches[:start_batch])
            if skipped:
                log.info("Resume: пропускаю %d готовых шагов (%d батчей)",
                         skipped, start_batch)
                self.on_progress(batches[start_batch][0][0] if start_batch < len(batches) else total,
                                 total, "Продолжаю с места падения",
                                 f"{skipped} шагов уже готово")

        log.info("Scenario start: %d steps, output_dir=%s", total, self.output_dir)
        try:
            done = sum(len(b) for b in batches[:start_batch])
            for batch_no, batch in enumerate(batches):
                if batch_no < start_batch:
                    continue
                self._check_cancel()
                if len(batch) == 1:
                    idx, step = batch[0]
                    self._current_idx = idx
                    label = step.get("name") or step.get("type")
                    self.on_progress(idx, total, label)
                    log.info("Step %d/%d START: type=%s name=%r", idx + 1, total, step.get("type"), label)
                    try:
                        await self._run_step(idx, step)
                    except ScenarioCancelled:
                        raise
                    except StepSkipped:
                        log.warning("Step %d/%d SKIPPED by user: type=%s name=%r",
                                    idx + 1, total, step.get("type"), label)
                        self._set_output(step, "")
                        self.on_progress(idx, total, label, "Пропущен пользователем")
                    except Exception as e:
                        # If we were cancelled and a Playwright/HTTP call
                        # blew up because of it, convert to cancellation.
                        if self._cancelled():
                            log.info("Step %d/%d aborted by cancel: %s", idx + 1, total, e)
                            raise ScenarioCancelled() from e
                        log.exception("Step %d/%d FAILED: type=%s name=%r", idx + 1, total, step.get("type"), label)
                        raise
                    log.info("Step %d/%d OK: type=%s", idx + 1, total, step.get("type"))
                    done += 1
                    self._write_checkpoint(batch_no + 1)
                else:
                    # Run all steps in batch concurrently
                    names = " + ".join(s.get("name") or s.get("type") for _, s in batch)
                    self._current_idx = batch[0][0]
                    self.on_progress(batch[0][0], total, f"Параллельно: {names}")
                    log.info("Parallel batch START: %s", names)
                    # Если один шаг батча падает, соседей нужно явно отменить и
                    # дождаться: иначе они остаются висеть «осиротевшими» и
                    # продолжают тратить кредиты/время после ошибки.
                    # Пропуск (StepSkipped) гасим на уровне шага — пропуск
                    # одного шага не должен сносить соседей по батчу.
                    batch_tasks = [
                        asyncio.create_task(self._run_step_skippable(idx, s))
                        for idx, s in batch
                    ]
                    try:
                        await asyncio.gather(*batch_tasks)
                    except BaseException as e:
                        for bt in batch_tasks:
                            if not bt.done():
                                bt.cancel()
                        await asyncio.gather(*batch_tasks, return_exceptions=True)
                        if isinstance(e, ScenarioCancelled):
                            raise
                        if self._cancelled():
                            log.info("Parallel batch aborted by cancel: %s", e)
                            raise ScenarioCancelled() from e
                        log.exception("Parallel batch FAILED: %s", names)
                        raise
                    log.info("Parallel batch OK: %s", names)
                    done += len(batch)
                    self._write_checkpoint(batch_no + 1)
        finally:
            await self._close_all_ai()
        self._clear_checkpoint()  # успешный финиш — возобновлять нечего
        self.on_progress(total, total, "Готово")
        return self.vars

    def _resume_start_batch(self, batches: list) -> int:
        """С какого батча продолжать по чекпоинту.

        Нюанс AI-чатов: шаги *_prompt/*_ask/*_chunked живут в контексте
        чата, открытого шагом {provider}_open. Если первый невыполненный
        чат-шаг провайдера опирается на чат, открытый ДО точки
        возобновления, — чат мёртв (браузер закрыт при падении), и надо
        откатиться к его *_open и переиграть диалог. Провайдеры (Claude,
        ChatGPT) проверяются НЕЗАВИСИМО — берём самый ранний откат. Дорогая
        часть (озвучка, картинки) после диалога всё равно пропускается по
        чекпоинту."""
        want = self._resume_state.get("completed_batches") or 0
        start = max(0, min(int(want), len(batches)))
        rollback = start
        resolved = set()  # провайдеры, судьба которых уже ясна
        for b in batches[start:]:
            for _, s in b:
                t = s.get("type") or ""
                prov = ai_step_provider(t)
                if not prov or prov in resolved:
                    continue
                action = t.split("_", 1)[1]
                if action == "open":
                    resolved.add(prov)  # дальше чат откроется заново сам
                elif action in _AI_CHAT_ACTIONS:
                    resolved.add(prov)
                    for bi in range(start - 1, -1, -1):
                        if any(st.get("type") == f"{prov}_open"
                               for _, st in batches[bi]):
                            log.info("Resume: откат с батча %d на %d — чат %s "
                                     "нужно переиграть", start, bi,
                                     AI_PROVIDER_LABEL[prov])
                            rollback = min(rollback, bi)
                            break
                    else:
                        rollback = 0
            if len(resolved) == len(AI_PROVIDERS):
                break
        return rollback

    def _write_checkpoint(self, completed_batches: int):
        """Атомарно сохранить прогресс в папку результата. Ошибки глотаем:
        чекпоинт — страховка, он не должен ронять здоровый запуск."""
        try:
            safe_vars = {}
            for k, v in self.vars.items():
                try:
                    json.dumps(v)
                    safe_vars[k] = v
                except (TypeError, ValueError):
                    continue
            state = {
                "scenario_id": self.scenario.get("id"),
                "fingerprint": steps_fingerprint(self.scenario.get("steps", [])),
                "completed_batches": completed_batches,
                "vars": safe_vars,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            path = os.path.join(self.output_dir, CHECKPOINT_FILENAME)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            log.exception("Не смог записать чекпоинт")

    def _clear_checkpoint(self):
        try:
            os.remove(os.path.join(self.output_dir, CHECKPOINT_FILENAME))
        except OSError:
            pass

    async def _run_step_skippable(self, idx: int, step: dict):
        """_run_step, но StepSkipped гасится здесь же: используется в
        параллельных батчах, где пропуск одного шага не должен отменять
        соседние (gather отменил бы их при любом исключении)."""
        try:
            await self._run_step(idx, step)
        except StepSkipped:
            label = step.get("name") or step.get("type")
            log.warning("Step %d SKIPPED by user (parallel batch): %s", idx + 1, label)
            self._set_output(step, "")
            self.on_progress(idx, self._total_steps, label, "Пропущен пользователем")

    async def _run_step(self, idx: int, step: dict):
        t = step.get("type")
        out_key = step.get("output")

        if t == "yt_url":
            url = self.vars.get(out_key or "url")
            if not url and self.on_user_input:
                url = await self.on_user_input("Введите ссылку на YouTube видео")
            if not url:
                raise RuntimeError("YouTube URL не указан")
            self._set_output(step, url)

        elif t == "user_text":
            txt = self.vars.get(out_key or "text")
            if not txt and self.on_user_input:
                txt = await self.on_user_input("Введите текст")
            self._set_output(step, txt or "")

        elif t == "file":
            self._set_output(step, step.get("path") or "")

        elif t == "yt_title":
            from modules.transcript import get_title
            url = self._resolve(step.get("inputs", {}).get("url")) or self.vars.get("url")
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Заголовок", "Запрос к YouTube…")
            log.info("yt_title: fetching for %s", url)
            t0 = time.time()
            loop = asyncio.get_event_loop()
            try:
                title = await asyncio.wait_for(
                    loop.run_in_executor(None, get_title, url),
                    timeout=int(step.get("timeout", 180)),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "YouTube не отдал заголовок за отведённое время. "
                    "Скорее всего YouTube ограничивает доступ — подожди несколько минут или включи VPN."
                )
            log.info("yt_title: done in %.1fs", time.time() - t0)
            self._set_output(step, title)

        elif t == "yt_desc":
            from modules.transcript import get_description
            url = self._resolve(step.get("inputs", {}).get("url")) or self.vars.get("url")
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Описание", "Запрос к YouTube…")
            log.info("yt_desc: fetching for %s", url)
            t0 = time.time()
            loop = asyncio.get_event_loop()
            try:
                desc = await asyncio.wait_for(
                    loop.run_in_executor(None, get_description, url),
                    timeout=int(step.get("timeout", 180)),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "YouTube не отдал описание за отведённое время. "
                    "Скорее всего YouTube ограничивает доступ — подожди несколько минут или включи VPN."
                )
            log.info("yt_desc: done in %.1fs", time.time() - t0)
            self._set_output(step, desc)

        elif t == "yt_transcript":
            from modules.transcript import get_transcript
            url = self._resolve(step.get("inputs", {}).get("url")) or self.vars.get("url")
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Транскрипт",
                             "Запрашиваю транскрипт у YouTube…")
            log.info("yt_transcript: fetching for %s", url)
            t0 = time.time()
            loop = asyncio.get_event_loop()
            try:
                text = await asyncio.wait_for(
                    loop.run_in_executor(None, get_transcript, url),
                    timeout=int(step.get("timeout", 180)),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "YouTube не отдал транскрипт за отведённое время. "
                    "Возможно, YouTube ограничил доступ — попробуй ещё раз через "
                    "несколько минут или используй VPN."
                )
            log.info("yt_transcript: done in %.1fs (%d chars)", time.time() - t0, len(text))
            self._set_output(step, text)

        elif t == "yt_preview":
            from modules.transcript import download_thumbnail
            url = self._resolve(step.get("inputs", {}).get("url")) or self.vars.get("url")
            path = os.path.join(self.output_dir, step.get("filename") or "thumbnail.jpg")
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Превью", "Скачивание превью…")
            log.info("yt_preview: fetching for %s", url)
            t0 = time.time()
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, download_thumbnail, url, path),
                    timeout=int(step.get("timeout", 180)),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "YouTube не отдал превью за отведённое время. "
                    "Скорее всего YouTube ограничивает доступ — подожди несколько минут или включи VPN."
                )
            log.info("yt_preview: done in %.1fs", time.time() - t0)
            self._set_output(step, path)

        elif t in ("claude_open", "gpt_open"):
            provider = ai_step_provider(t)
            label = AI_PROVIDER_LABEL[provider]
            # Reuse the existing browser if it's still alive — restarting the
            # persistent Chrome context too quickly causes a lock race that
            # crashes the new instance a few seconds after launch.
            sess = self._ai_sessions.get(provider)
            if sess and sess.is_alive():
                log.info("Reusing existing %s browser for new chat", label)
                await sess.new_chat()
            else:
                if sess:
                    log.info("Existing %s browser is dead — restarting", label)
                    await self._close_ai(provider)
                sess = self._new_ai_session(provider)
                self._ai_sessions[provider] = sess
                await sess.start(
                    status_cb=lambda m: self.on_progress(
                        idx, self._total_steps,
                        step.get("name") or label, m))
            if provider == "claude":
                # Always explicitly switch to a model (defaults to Opus 4.8 for legacy
                # scenarios without an explicit `model` field). Belt-and-suspenders:
                # we'd rather click-through every time than trust the browser's
                # current default. Effort выставляется в том же проходе через UI
                # (low|medium|high|xhigh|max). Если effort пустой — не трогаем.
                model = (step.get("model") or "Opus 4.8").strip()
                # Маппинг устаревших имён — после обновлений Claude UI они исчезают
                # из дропдауна. Без алиаса set_model_and_effort молча не найдёт пункт.
                MODEL_ALIASES = {"Opus 4.7": "Opus 4.8", "Opus 4.6": "Opus 4.8",
                                 "Sonnet 4.6": "Sonnet 5", "Sonnet 4.5": "Sonnet 5"}
                model = MODEL_ALIASES.get(model, model)
                effort = (step.get("effort") or "").strip().lower() or None
                try:
                    ok = await sess.set_model_and_effort(model, effort)
                    if not ok:
                        # Не падаем, но проблема серьёзная — без правильной
                        # модели/effort пайплайн уйдёт в дефолт браузера. Поднимаем
                        # explicit ошибку, чтобы UI показал её сразу.
                        raise RuntimeError(
                            f"Не удалось переключить Claude на {model}"
                            + (f" / effort={effort}" if effort else "")
                            + ". Проверь что в браузере открыт claude.ai/new (не login и не challenge), "
                            "и повтори запуск."
                        )
                except RuntimeError:
                    raise
                except Exception as e:
                    log.warning("Failed to switch model/effort '%s'/'%s': %s",
                                model, effort, e)
            else:
                # ChatGPT: меню «Intelligence» двухуровневое (июль 2026) —
                # скорость мышления (Instant/Medium/High) наверху, модель
                # (GPT-5.5/…/o3) в подменю; и то и другое выбирается одним
                # sess.set_model(). Выбор МЯГКИЙ: без подписки пунктов может
                # не быть — предупреждаем и едем дальше, hard-fail сделал бы
                # сценарии неработоспособными без подписки.
                model = (step.get("model") or "").strip()
                # Пункты старого меню (до июля 2026) — маппим на ближайший
                # смысл: Thinking = «думай дольше» → High; Auto = дефолт.
                # GPT-5.4 OpenAI отключает 23.07.2026 («Leaving on July 23»)
                # — старые сценарии с ней переводим на GPT-5.5.
                GPT_ALIASES = {"Auto": "", "Thinking": "High",
                               "GPT-5.4": "GPT-5.5"}
                model = GPT_ALIASES.get(model, model)
                effort = (step.get("effort") or "").strip()
                for value in (model, effort):
                    if not value:
                        continue
                    try:
                        ok = await sess.set_model(value)
                        if not ok:
                            log.warning("Не удалось выбрать «%s» в меню ChatGPT "
                                        "— продолжаю на настройках по умолчанию",
                                        value)
                            self.on_progress(
                                idx, self._total_steps, step.get("name") or label,
                                f"«{value}» недоступно в меню ChatGPT (нет "
                                "подписки?) — работаю на настройках по умолчанию")
                    except Exception as e:
                        log.warning("Failed to switch GPT option '%s': %s",
                                    value, e)

        elif t in ("claude_close", "gpt_close"):
            await self._close_ai(ai_step_provider(t))

        elif t in ("claude_prompt", "gpt_prompt"):
            provider = ai_step_provider(t)
            sess = self._require_ai_session(provider)
            tpl = step.get("prompt_template", "")
            prompt = substitute(tpl, self.vars)
            file_paths = step.get("file_paths") or []
            file_paths = [substitute(p, self.vars) if isinstance(p, str) else p for p in file_paths]
            file_paths = [p for p in file_paths if p and os.path.exists(p)]
            timeout = int(step.get("timeout", 600))
            await sess.send_message(prompt, file_paths or None)
            reply = await sess.wait_for_response(timeout=timeout, is_cancelled=self._cancelled)
            self._set_output(step, reply)
            # Remember which var carries the most recent reply of this provider
            out_var = step.get("output")
            if out_var:
                self._last_reply_var[provider] = out_var

        elif t in ("claude_ask", "gpt_ask"):
            provider = ai_step_provider(t)
            sess = self._require_ai_session(provider)
            # Pick which variable to display:
            # 1) explicit show_var on the step
            # 2) the most recent reply variable of this provider (tracked)
            # 3) legacy fallback {provider}_reply_1
            show_var = (step.get("show_var") or self._last_reply_var[provider]
                        or f"{provider}_reply_1")
            last_reply = self.vars.get(show_var, "")
            if self.on_ask_user:
                user_msg = await self.on_ask_user(last_reply, provider)
            else:
                user_msg = ""
            if user_msg:
                timeout = int(step.get("timeout", 1200))
                await sess.send_message(user_msg)
                ai_reply = await sess.wait_for_response(timeout=timeout, is_cancelled=self._cancelled)
                self._set_output(step, ai_reply)
                out_var = step.get("output")
                if out_var:
                    self._last_reply_var[provider] = out_var
            else:
                self._set_output(step, "")

        elif t in ("claude_chunked", "gpt_chunked"):
            provider = ai_step_provider(t)
            sess = self._require_ai_session(provider)
            step_name = step.get("name") or "Перевод по кускам"
            # Источник — переменная с полным оригиналом.
            original = self._resolve_text_source(step)
            if isinstance(original, (list, tuple)):
                original = "\n".join(str(x) for x in original)
            elif not isinstance(original, str):
                original = "" if original is None else str(original)
            if not original.strip():
                raise RuntimeError(
                    f"Источник «{step.get('source') or '—'}» пуст — нечего резать на куски")

            lo, hi = CHUNK_SIZES.get(step.get("chunk_size") or DEFAULT_CHUNK_SIZE,
                                     CHUNK_SIZES[DEFAULT_CHUNK_SIZE])
            chunks = chunk_text(original, lo, hi)
            total = len(chunks)
            if total == 0:
                raise RuntimeError("Не удалось нарезать текст на куски (источник пуст?)")
            try:
                timeout = int(step.get("timeout") or 600)
            except (TypeError, ValueError):
                timeout = 600

            # 1) Вступление: весь оригинал + правила. {original} = весь текст.
            intro = substitute(step.get("intro_template") or "",
                               {**self.vars, "original": original})
            if intro.strip():
                self.on_progress(idx, self._total_steps, step_name, "Отправляю контекст…")
                await sess.send_message(intro)
                await sess.wait_for_response(timeout=timeout, is_cancelled=self._cancelled)

            # 2) Один txt: создаём пустым, дальше дописываем после каждого
            # ответа — упадёт на середине, готовое сохранится.
            filename = substitute(step.get("filename") or "result.txt", self.vars)
            path = os.path.join(self.output_dir, filename)
            open(path, "w", encoding="utf-8").close()

            chunk_tpl = step.get("chunk_template") or "{chunk}"
            combined = []
            for i, chunk in enumerate(chunks, 1):
                if self._cancelled():
                    raise ScenarioCancelled()
                if self._skip_requested(idx):
                    raise StepSkipped()
                self.on_progress(idx, self._total_steps, step_name,
                                 f"Кусок {i}/{total} (~{len(chunk)} симв.)")
                msg = substitute(chunk_tpl, {**self.vars, "original": original,
                                             "chunk": chunk, "n": i, "total": total})
                await sess.send_message(msg)
                reply = await sess.wait_for_response(
                    timeout=timeout, is_cancelled=self._cancelled)
                # Дописываем сразу; между фрагментами пустая строка.
                with open(path, "a", encoding="utf-8") as f:
                    if i > 1:
                        f.write("\n\n")
                    f.write(reply or "")
                combined.append(reply or "")
            self._set_output(step, "\n\n".join(combined))
            out_var = step.get("output")
            if out_var:
                self._last_reply_var[provider] = out_var

        elif t == "save_txt":
            content = self._resolve_text_source(step)
            # Source may resolve to a list (e.g. image/video paths) or other
            # non-string; coerce so f.write() can't blow up the whole run.
            if isinstance(content, (list, tuple)):
                content = "\n".join(str(x) for x in content)
            elif not isinstance(content, str):
                content = "" if content is None else str(content)
            filename = substitute(step.get("filename") or "output.txt", self.vars)
            path = os.path.join(self.output_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._set_output(step, content)

        elif t == "save_json":
            content = self._resolve_text_source(step)
            filename = substitute(step.get("filename") or "output.json", self.vars)
            path = os.path.join(self.output_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                if isinstance(content, (dict, list)):
                    json.dump(content, f, ensure_ascii=False, indent=2)
                else:
                    f.write(content or "")
            self._set_output(step, content if isinstance(content, (dict, list)) else (content or ""))

        elif t == "voice":
            from modules.voice_api import synthesize, VoiceCancelled, VoiceSkipped
            preset = step.get("preset")
            text = self._resolve_text_source(step)
            filename = substitute(step.get("filename") or "voice.mp3", self.vars)
            path = os.path.join(self.output_dir, filename)
            step_name = step.get("name") or "Озвучка"
            # Дозапуск: готовый mp3 из прошлой попытки не переозвучиваем —
            # это оплаченный результат (актуально, когда в параллельном батче
            # озвучка успела, а сосед упал и батч не дошёл до чекпоинта).
            if self._resume_state:
                try:
                    if os.path.getsize(path) > 0:
                        log.info("Resume: %s уже озвучен — пропускаю синтез", filename)
                        self.on_progress(idx, self._total_steps, step_name,
                                         "Готово ранее — пропущено")
                        self._set_output(step, path)
                        return
                except OSError:
                    pass
            # Пишем статус задачи озвучки в прогресс пайплайна (для csv666 это
            # status_label сервиса: «В очереди», «Синтез…», «Готово»).
            def _voice_status(msg, _i=idx, _n=step_name):
                self.on_progress(_i, self._total_steps, _n, msg)
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None, lambda: synthesize(
                        text, preset, path,
                        cancel_check=self._cancelled,
                        skip_check=lambda: self._skip_requested(idx),
                        status_callback=_voice_status)
                )
            except VoiceCancelled as e:
                raise ScenarioCancelled() from e
            except VoiceSkipped as e:
                raise StepSkipped() from e
            self._set_output(step, path)

        elif t == "banana_one":
            from modules import veo_api
            prompt = substitute(step.get("prompt") or "", self.vars)
            count = int(step.get("count", 1))
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Картинка",
                             f"Генерация {count} шт через {step.get('model', 'GEM_PIX_2')}…")
            data = await veo_api.banana_generate_with_retry(
                prompt=prompt,
                num_images=count,
                model_key=step.get("model", "GEM_PIX_2"),
                aspect_ratio=step.get("aspect_ratio", "16:9"),
            )
            self.on_progress(idx, self._total_steps,
                             step.get("name") or "Картинка", "Скачивание…")
            # Unique prefix per step so two banana_one steps in a parallel batch
            # don't overwrite each other's image_NNN.png files.
            paths = await self._save_banana(data, prefix=f"image_{step.get('id') or idx}")
            self._set_output(step, paths)

        elif t == "banana_batch":
            from modules import veo_api
            source_var = step.get("source") or "image_prompts"
            raw = self.vars.get(source_var, "")
            prompts = [p.strip() for p in str(raw).splitlines() if p.strip()]
            if not prompts:
                raise RuntimeError(f"Источник промптов {{{source_var}}} пуст")

            model_key = step.get("model", "GEM_PIX_2")
            aspect_ratio = step.get("aspect_ratio", "16:9")
            total = len(prompts)
            step_name = step.get("name") or "Картинки"
            total_steps = len(self.scenario.get("steps", []))

            # Concurrency: explicit override in step, else from user's plan.
            concurrency_override = step.get("concurrency")
            if concurrency_override:
                try:
                    concurrency = max(1, int(concurrency_override))
                except (TypeError, ValueError):
                    concurrency = veo_api.get_concurrency_limit(default=4)
            else:
                loop = asyncio.get_event_loop()
                concurrency = await loop.run_in_executor(
                    None, veo_api.get_concurrency_limit, 4
                )
            log.info("banana_batch: %d prompts, concurrency=%d", total, concurrency)

            sem = asyncio.Semaphore(concurrency)
            done_count = 0
            fail_count = 0
            done_lock = asyncio.Lock()
            results: list[list] = [[] for _ in prompts]
            failures: list[tuple[int, str, str]] = []  # (index, prompt, error)

            def _is_permanent(err: Exception) -> bool:
                """Google's INVALID_ARGUMENT / safety-filter rejects never
                succeed on retry — recognising them stops infinite loops."""
                msg = str(err)
                markers = ("INVALID_ARGUMENT", "BAD_REQUEST", "safety",
                           "SAFETY", "blocked", "PROHIBITED")
                return any(m in msg for m in markers)

            def _backoff(attempt: int) -> int:
                # 5, 10, 20, 30, then 60 forever
                seq = (5, 10, 20, 30)
                return seq[attempt - 1] if attempt <= len(seq) else 60

            async def _sleep_cancellable(sec: int):
                # Wake up every 0.5s to check for cancellation / skip.
                for _ in range(sec * 2):
                    self._check_cancel()
                    self._check_skip(idx)
                    await asyncio.sleep(0.5)

            # Дозапуск: картинки, скачанные прошлой попыткой, не генерируем
            # заново — сверяемся по префиксу img_NNN в папке images.
            resume_ready: dict[int, list] = {}
            if self._resume_state:
                images_dir = os.path.join(self.output_dir, "images")
                if os.path.isdir(images_dir):
                    for i in range(1, total + 1):
                        prefix = f"img_{i:03d}_"
                        found = sorted(
                            os.path.join(images_dir, n)
                            for n in os.listdir(images_dir)
                            if n.startswith(prefix) and os.path.getsize(
                                os.path.join(images_dir, n)) > 0
                        )
                        if found:
                            resume_ready[i] = found
                if resume_ready:
                    log.info("Resume: %d/%d картинок уже скачаны — пропускаю их",
                             len(resume_ready), total)

            async def _one(i: int, prompt: str):
                nonlocal done_count, fail_count
                ready = resume_ready.get(i)
                if ready:
                    results[i - 1] = ready
                    async with done_lock:
                        done_count += 1
                    return
                # Don't take a slot if we're already cancelled — saves credits
                # on the prompts that haven't started yet.
                self._check_cancel()
                self._check_skip(idx)
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        async with sem:
                            self._check_cancel()
                            self._check_skip(idx)
                            data = await veo_api.banana_generate_batch(
                                prompt=prompt,
                                num_images=1,
                                model_key=model_key,
                                aspect_ratio=aspect_ratio,
                            )
                            paths = await self._save_banana(data, prefix=f"img_{i:03d}")
                            results[i - 1] = paths
                        async with done_lock:
                            done_count += 1
                            self.on_progress(
                                idx, total_steps, step_name,
                                f"{done_count}/{total} готово"
                                + (f" ({fail_count} ошибок)" if fail_count else ""),
                            )
                        return
                    except (ScenarioCancelled, StepSkipped):
                        raise
                    except Exception as e:
                        # Permanent rejects (Google safety filter etc.) —
                        # give up after 2 attempts, no point retrying forever.
                        if _is_permanent(e) and attempt >= 2:
                            log.error(
                                "banana_batch: prompt #%d permanently rejected by Google "
                                "after %d attempts: %s. Prompt: %.500s",
                                i, attempt, e, prompt,
                            )
                            async with done_lock:
                                fail_count += 1
                                failures.append((i, prompt, str(e)))
                                self.on_progress(
                                    idx, total_steps, step_name,
                                    f"{done_count}/{total} готово ({fail_count} ошибок)",
                                )
                            return
                        # Transient (500, network, timeout) — retry forever
                        # with backoff. Cancellation breaks out of sleep.
                        wait = _backoff(attempt)
                        log.warning(
                            "banana_batch: prompt #%d failed (attempt %d): %s — retry in %ds. Prompt: %.200s",
                            i, attempt, e, wait, prompt,
                        )
                        async with done_lock:
                            self.on_progress(
                                idx, total_steps, step_name,
                                f"{done_count}/{total} готово"
                                + (f" ({fail_count} ошибок)" if fail_count else "")
                                + f" • промпт #{i}: попытка {attempt}, ждём {wait}с…",
                            )
                        await _sleep_cancellable(wait)

            self.on_progress(idx, total_steps, step_name,
                             f"0/{total} (параллельно: {concurrency})")
            tasks = [
                asyncio.create_task(_one(i, p))
                for i, p in enumerate(prompts, 1)
            ]
            try:
                await asyncio.gather(*tasks)
            except ScenarioCancelled:
                for tk in tasks:
                    if not tk.done():
                        tk.cancel()
                raise
            except StepSkipped:
                # Пропуск шага: глушим оставшиеся генерации, но уже скачанные
                # картинки сохраняем как результат — терять их нет смысла.
                for tk in tasks:
                    if not tk.done():
                        tk.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                partial = [p for sub in results for p in sub]
                log.warning("banana_batch: пропущен пользователем (%d/%d картинок готово)",
                            len(partial), total)
                self.on_progress(idx, total_steps, step_name,
                                 f"Пропущен ({len(partial)}/{total} готово)")
                self._set_output(step, partial)
                return

            all_paths = [p for sub in results for p in sub]

            if failures:
                log.warning(
                    "banana_batch: finished with %d/%d failures",
                    len(failures), total,
                )
                if not all_paths:
                    # Every single prompt failed — surface the first error so
                    # the scenario stops with a clear cause.
                    first_idx, _, first_err = failures[0]
                    raise RuntimeError(
                        f"Все {total} картинок не сгенерировались. "
                        f"Первая ошибка (промпт #{first_idx}): {first_err}"
                    )
                # Some succeeded — finish step OK but tell the user.
                self.on_progress(
                    idx, total_steps, step_name,
                    f"Готово {len(all_paths)}/{total}. {len(failures)} промптов "
                    f"не удалось — см. vizo.log",
                )

            self._set_output(step, all_paths)

        elif t == "video_t2v":
            from modules import veo_api
            prompt = substitute(step.get("prompt") or step.get("prompt_template") or "", self.vars)
            loop = asyncio.get_event_loop()
            task_id = await loop.run_in_executor(
                None, veo_api.text_to_video,
                prompt, step.get("aspect_ratio", "16:9"),
                int(step.get("count", 1)), step.get("duration", "8s"),
            )
            paths = await self._fetch_video(task_id, step.get("name") or "Видео",
                                            step_idx=idx)
            self._set_output(step, paths)

        elif t == "video_i2v":
            from modules import veo_api
            prompt = substitute(step.get("prompt") or "", self.vars)
            image_path = step.get("image_path") or self.vars.get(step.get("image_var", ""))
            if not image_path or not os.path.exists(image_path):
                raise RuntimeError("Картинка для image→video не найдена")
            b64, mime = veo_api.file_to_base64(image_path)
            loop = asyncio.get_event_loop()
            task_id = await loop.run_in_executor(
                None, veo_api.image_to_video,
                prompt, b64, mime,
                step.get("aspect_ratio", "16:9"),
                int(step.get("count", 1)), step.get("duration", "8s"),
            )
            paths = await self._fetch_video(task_id, step.get("name") or "Видео",
                                            step_idx=idx)
            self._set_output(step, paths)

        elif t == "video_batch":
            from modules import veo_api
            prompt = substitute(step.get("prompt") or "", self.vars)
            start = step.get("start_image_path")
            end = step.get("end_image_path")
            if not start or not end:
                raise RuntimeError("Нужны стартовая и конечная картинки")
            b64s, _ = veo_api.file_to_base64(start)
            b64e, _ = veo_api.file_to_base64(end)
            loop = asyncio.get_event_loop()
            task_id = await loop.run_in_executor(
                None, veo_api.batch_frame, prompt, b64s, b64e,
                step.get("aspect_ratio", "16:9"), int(step.get("count", 1)),
            )
            paths = await self._fetch_video(task_id, step.get("name") or "Видео",
                                            step_idx=idx)
            self._set_output(step, paths)

        else:
            raise RuntimeError(f"Неизвестный тип шага: {t}")

    def _resolve(self, value):
        if isinstance(value, str):
            return substitute(value, self.vars)
        return value

    def _resolve_text_source(self, step: dict):
        """Resolve the 'source' field — typically a variable name or
        substituted template."""
        src = step.get("source")
        if not src:
            return ""
        # If source is a bare variable name (without braces), look it up
        if isinstance(src, str) and src and "{" not in src:
            if src in self.vars:
                return self.vars[src]
            return src
        return substitute(src or "", self.vars)

    def _set_output(self, step: dict, value):
        out = step.get("output")
        if out:
            self.vars[out] = value

    def _new_ai_session(self, provider: str):
        """Создать браузерную сессию провайдера. Импорты локальные — как и
        раньше у Claude: playwright тянется только когда реально нужен."""
        if provider == "claude":
            from modules.claude_ui import ClaudeAutomation
            return ClaudeAutomation()
        if provider == "gpt":
            from modules.gpt_ui import GPTAutomation
            return GPTAutomation()
        raise RuntimeError(f"Неизвестный AI-провайдер: {provider}")

    def _require_ai_session(self, provider: str):
        sess = self._ai_sessions.get(provider)
        if not sess:
            raise RuntimeError(
                f"Нет открытого чата {AI_PROVIDER_LABEL[provider]}. "
                "Добавь шаг «Открыть новый чат»")
        return sess

    async def _close_ai(self, provider: str):
        sess = self._ai_sessions.get(provider)
        if sess:
            try:
                await sess.close()
            except Exception:
                pass
            self._ai_sessions[provider] = None

    async def _close_all_ai(self):
        for provider in AI_PROVIDERS:
            await self._close_ai(provider)

    async def _save_banana(self, data: dict, prefix: str = "image") -> list:
        import aiohttp
        media = data.get("media") or []
        paths = []
        images_dir = os.path.join(self.output_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        existing = len([f for f in os.listdir(images_dir) if f.startswith(prefix)])
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(media, 1):
                url = item.get("fifeUrl") or item.get("url")
                if not url:
                    continue
                path = os.path.join(images_dir, f"{prefix}_{existing + i:03d}.png")
                async with session.get(url) as r:
                    r.raise_for_status()
                    content = await r.read()
                # Атомарно: оборванное скачивание не должно оставить битый PNG,
                # который при повторном запуске посчитается готовым.
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(content)
                os.replace(tmp, path)
                paths.append(path)
        return paths

    async def _fetch_video(self, task_id: str, label: str = "Видео",
                           step_idx: int = None) -> list:
        """Poll the VeoNonStop task until completion. If the user cancels
        during the wait, we call /video/cancel/{task_id} server-side so no
        more compute / credits are spent, then propagate the cancellation.
        Пропуск шага (step_idx) делает то же самое, но поднимает StepSkipped —
        сценарий продолжается дальше без этого видео.
        """
        from modules import veo_api
        loop = asyncio.get_event_loop()
        # Track this task so request_cancel() / cleanup can find it.
        self._active_video_tasks.add(task_id)
        # Жёсткий потолок ожидания + допуск на временные сбои опроса: один
        # сетевой глюк не должен убивать 30-минутный оплаченный рендер.
        deadline = time.time() + veo_api.VIDEO_TIMEOUT_SEC + 300
        poll_failures = 0

        def _skip_now() -> bool:
            return step_idx is not None and self._skip_requested(step_idx)

        try:
            result = None
            while True:
                if self._cancelled() or _skip_now():
                    skipping = not self._cancelled()
                    log.info("Cancelling VeoNonStop task %s server-side (%s)",
                             task_id, "skip" if skipping else "cancel")
                    try:
                        await loop.run_in_executor(None, veo_api.cancel_task, task_id)
                    except Exception as e:
                        log.warning("Failed to cancel VeoNonStop task %s: %s", task_id, e)
                    raise StepSkipped() if skipping else ScenarioCancelled()
                if time.time() > deadline:
                    raise RuntimeError(
                        f"VeoNonStop задача {task_id} не завершилась за отведённое время"
                    )
                try:
                    data = await loop.run_in_executor(None, veo_api.get_video_status, task_id)
                    poll_failures = 0
                except Exception as e:
                    poll_failures += 1
                    log.warning("Опрос статуса видео %s не удался (%d/%d): %s",
                                task_id, poll_failures, veo_api.MAX_POLL_FAILURES, e)
                    if poll_failures >= veo_api.MAX_POLL_FAILURES:
                        raise
                    await asyncio.sleep(veo_api.VIDEO_POLL_INTERVAL_SEC)
                    continue
                status = str(data.get("status", "")).upper()
                prog = data.get("progress") or {}
                done = prog.get("completed") or 0
                total = prog.get("total") or 1
                self.on_progress(self._current_idx, self._total_steps,
                                 label, f"{status} {done}/{total}")
                if status == "COMPLETED":
                    result = data
                    break
                if status == "FAILED":
                    raise RuntimeError(f"VeoNonStop задача упала: {data.get('error', 'unknown')}")
                if status == "CANCELLED":
                    # Отменено на сервере. Если отменял не пользователь —
                    # это ошибка, а не «успешная отмена».
                    if self._cancelled():
                        raise ScenarioCancelled()
                    raise RuntimeError(f"VeoNonStop задача {task_id} отменена на сервере")
                # Sleep in small slices so cancel/skip is responsive
                for _ in range(int(veo_api.VIDEO_POLL_INTERVAL_SEC * 2)):
                    if self._cancelled() or _skip_now():
                        break
                    await asyncio.sleep(0.5)
        finally:
            self._active_video_tasks.discard(task_id)
        videos = result.get("videos") or []
        videos_dir = os.path.join(self.output_dir, "videos")
        os.makedirs(videos_dir, exist_ok=True)
        existing = len(os.listdir(videos_dir))
        paths = []
        for i in range(len(videos)):
            self._check_cancel()
            path = os.path.join(videos_dir, f"video_{existing + i + 1:02d}.mp4")
            await loop.run_in_executor(None, veo_api.download_video, task_id, path, i)
            paths.append(path)
        return paths
