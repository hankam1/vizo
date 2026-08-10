"""Третья озвучка — «test» (VoiceGen, https://qw1voicegencore.pro).

В UI движок называется **test** (рабочее имя, заменим когда сервис получит
финальное название). Внутренний код движка — `voicegen`, менять его нельзя:
он записан в сохранённых голосах пользователя (тот же приём, что с csv666 /
VoiceBot).

Голос собирается из настроек прямо здесь (как у Озвучки Матея), но шкала
параметров у сервиса своя, целочисленная: speed 70–120 (100 = 1.0x),
stability/similarity/style — 0–100. Альтернатива настройкам — `settings_preset`
(живость) или `template_id` шаблона из Telegram-бота сервиса.

Поток: POST /api/v1/client/tasks → GET /api/v1/client/tasks/{id} (пока не
`done`) → GET /api/v1/client/tasks/{id}/download (может ответить 307).
Авторизация — `Authorization: Bearer <key>`.
"""
import os
import time
import requests

from config import VOICE_VOICEGEN_BASE
from modules import settings

# Имя движка, как его видит пользователь (в логах/ошибках/статусах).
SERVICE_NAME = "test"
# Те же исключения, что и у остальных движков, чтобы пайплайн/раннер ловил их
# единообразно (отмена запуска и пропуск шага озвучки).
from modules.voice_api import VoiceCancelled, VoiceSkipped

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 30
DOWNLOAD_TIMEOUT_SEC = 600
# Сколько подряд неудачных опросов терпим до падения (см. voice_api.py).
MAX_CONSECUTIVE_POLL_FAILURES = 20

DONE_STATUSES = {"done", "completed", "success"}
ERROR_STATUSES = {"error", "failed", "cancelled", "canceled"}
STATUS_LABELS_RU = {
    "queued": "В очереди",
    "running": "Синтез…",
    "done": "Готово",
    "error": "Ошибка",
    "cancelled": "Отменено",
}

# Движки генерации сервиса и их РУЧНЫЕ параметры (voice_engine_settings).
# Параметры вне списка сервис для этой модели игнорирует — не шлём их вовсе,
# чтобы в задаче не оседал мусор.
ENGINE_PARAMS = {
    "elevenLabsV3": ("speed", "stability"),
    "elevenLabs": ("speed", "similarity", "stability", "style", "use_speaker_boost"),
    "fish": ("speed", "stability"),
    "auto": ("speed",),
    "panda": ("speed",),
    "starfish": ("speed",),
}
VOICE_ENGINES = list(ENGINE_PARAMS.keys())
SETTINGS_PRESETS = ["standard", "stable", "expressive", "fast"]

# Акцент поддерживает только elevenLabsV3; для других моделей сервис вернёт 400.
ACCENT_ENGINE = "elevenLabsV3"


def _headers() -> dict:
    key = settings.load().get("voice_api_key_voicegen", "")
    if not key:
        raise RuntimeError(f"API-ключ озвучки «{SERVICE_NAME}» не указан в настройках")
    return {"Authorization": f"Bearer {key}"}


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def build_payload(text: str, voice: dict, filename: str) -> dict:
    """Собирает тело POST /tasks из полей голоса (префикс `vg_`).

    Два взаимоисключающих режима:

    * **по шаблону** (`vg_template_id`) — шаблон из Telegram-бота уже содержит
      голос, модель, её настройки, пресет, режим разбивки, размер чанка и паузу.
      Досылать их из vizo нельзя: они перебили бы шаблон, ради которого его и
      заводили. Из запроса берутся только текст, имя файла и число потоков —
      ровно как написано в доке. Исключение — `settings_preset`: дока прямо
      говорит, что пресет из запроса переопределяет настройки шаблона, так что
      осознанно выбранный пресет отправляем.
    * **по настройкам** (`vg_voice_id`) — голос собирается здесь целиком.
    """
    template_id = (voice.get("vg_template_id") or "").strip()
    voice_id = (voice.get("vg_voice_id") or "").strip()
    if not template_id and not voice_id:
        raise RuntimeError(
            f"Для озвучки «{SERVICE_NAME}» нужен voice_id из каталога или ID шаблона"
        )

    payload = {"text": text, "filename": filename}
    preset = (voice.get("vg_settings_preset") or "").strip()

    if template_id:
        payload["template_id"] = template_id
        if preset:
            payload["settings_preset"] = preset
    else:
        engine = str(voice.get("vg_voice_engine") or "elevenLabsV3")
        payload["voice_engine"] = engine
        payload["voice_id"] = voice_id
        # Пресет живости заменяет ручные настройки — шлём что-то одно.
        if preset:
            payload["settings_preset"] = preset
        else:
            allowed = ENGINE_PARAMS.get(engine, ("speed",))
            src = voice.get("vg_settings") or {}
            vs = {}
            for key in allowed:
                if key == "use_speaker_boost":
                    if "use_speaker_boost" in src:
                        vs["use_speaker_boost"] = bool(src["use_speaker_boost"])
                    continue
                val = _int_or_none(src.get(key))
                if val is not None:
                    vs[key] = val
            if vs:
                payload["voice_engine_settings"] = vs

        # Акцент — только у elevenLabsV3, иначе сервис отвечает 400.
        accent = (voice.get("vg_accent") or "").strip()
        if accent and engine == ACCENT_ENGINE:
            payload["accent"] = accent

        chunk_size = _int_or_none(voice.get("vg_chunk_size"))
        if chunk_size:
            payload["chunk_size"] = chunk_size
        delay = voice.get("vg_delay_between_chunks")
        if delay not in (None, ""):
            try:
                payload["delay_between_chunks"] = float(delay)
            except (TypeError, ValueError):
                pass

    # Поля уровня задачи — работают в обоих режимах.
    if voice.get("vg_include_timestamps"):
        payload["include_timestamps"] = True
    threads = _int_or_none(voice.get("vg_thread_count"))
    if threads:
        payload["thread_count"] = threads
    return payload


def list_templates() -> list:
    """Шаблоны из Telegram-бота сервиса — их ID бот не показывает, отдаёт API.

    Возвращает `[{id, name, note}]` для выбора в UI.
    """
    r = requests.get(
        f"{VOICE_VOICEGEN_BASE}/api/v1/client/templates",
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка загрузки шаблонов")
    data = r.json()
    # Форма ответа в доке не зафиксирована — принимаем и голый список,
    # и любую из привычных обёрток.
    if isinstance(data, dict):
        for key in ("templates", "data", "items", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = []
    if not isinstance(data, list):
        return []
    out = []
    for t in data:
        if not isinstance(t, dict):
            continue
        tid = t.get("id") or t.get("template_id") or t.get("uuid")
        if not tid:
            continue
        note = " · ".join(str(t[k]) for k in ("voice_name", "voice_engine", "model")
                          if t.get(k))
        out.append({
            "id": str(tid),
            "name": str(t.get("name") or t.get("title") or tid),
            "note": note,
        })
    return out


def _api_error(r, action: str) -> Exception:
    """Понятные сообщения для типовых ответов сервиса."""
    if r.status_code == 401:
        return Exception(f"Неверный API-ключ озвучки «{SERVICE_NAME}»")
    if r.status_code == 403:
        return Exception(f"Подписка «{SERVICE_NAME}» не активна")
    if r.status_code == 404:
        return Exception(f"{action} «{SERVICE_NAME}»: не найден шаблон или голос ({r.text[:200]})")
    return Exception(f"{action} «{SERVICE_NAME}»: {r.status_code} {r.text[:300]}")


def _task_of(data: dict) -> dict:
    """Ответы приходят обёрнутыми в {"ok":..., "task": {...}}."""
    if isinstance(data, dict):
        task = data.get("task")
        if isinstance(task, dict):
            return task
        return data
    return {}


def _create_task(text: str, voice: dict, filename: str) -> dict:
    payload = build_payload(text, voice, filename)
    r = requests.post(
        f"{VOICE_VOICEGEN_BASE}/api/v1/client/tasks",
        json=payload,
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка создания задачи озвучки")
    task = _task_of(r.json())
    if not task.get("task_id"):
        raise Exception(f"{SERVICE_NAME} не вернул task_id: {r.text[:300]}")
    return task


def _status_text(task: dict) -> str:
    status = str(task.get("status", "")).lower()
    label = STATUS_LABELS_RU.get(status, status or "…")
    progress = task.get("progress")
    if progress is not None:
        try:
            pct = float(progress)
            pct = pct * 100 if pct <= 1 else pct
            return f"{label} {int(pct)}%"
        except (TypeError, ValueError):
            pass
    return label


def _wait_for_task(task_id: str, cancel_check=None, skip_check=None,
                   status_callback=None) -> dict:
    # Без дедлайна по времени: задача может законно висеть в очереди долго.
    # Выйти можно только отменой запуска или пропуском шага (как у voicer).
    last = ""
    consecutive_failures = 0
    while True:
        if cancel_check and cancel_check():
            raise VoiceCancelled("Озвучка отменена пользователем")
        if skip_check and skip_check():
            raise VoiceSkipped(
                f"Шаг озвучки пропущен пользователем (задача {task_id} "
                f"продолжит выполняться в «{SERVICE_NAME}»)"
            )
        try:
            r = requests.get(
                f"{VOICE_VOICEGEN_BASE}/api/v1/client/tasks/{task_id}",
                headers=_headers(),
                timeout=HTTP_TIMEOUT_SEC,
            )
            # 5xx/429 — временные проблемы сервера, опрос можно повторить.
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.RequestException(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise _api_error(r, "Ошибка статуса задачи")
            task = _task_of(r.json())
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise Exception(
                    f"Опрос статуса озвучки «{SERVICE_NAME}» не удался "
                    f"{consecutive_failures} раз подряд: {e}"
                )
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = str(task.get("status", "")).lower()
        line = _status_text(task)
        if line != last:
            if status_callback:
                try:
                    status_callback(line)
                except Exception:
                    pass
            print(f"  [{SERVICE_NAME}] {line}    ", end="\r", flush=True)
            last = line

        if status in DONE_STATUSES:
            print()
            return task
        if status in ERROR_STATUSES:
            print()
            msg = task.get("error") or task.get("message") or status
            raise Exception(f"Ошибка генерации «{SERVICE_NAME}»: {msg}")

        time.sleep(POLL_INTERVAL_SEC)


def _download_result(task_id: str, output_path: str) -> None:
    r = requests.get(
        f"{VOICE_VOICEGEN_BASE}/api/v1/client/tasks/{task_id}/download",
        headers=_headers(),
        allow_redirects=True,  # результат может лежать в S3 → 307 на прямую ссылку
        timeout=DOWNLOAD_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка скачивания")
    # Атомарная запись: оборванное скачивание не должно оставить битый mp3.
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, output_path)


def _download_timestamps(task: dict, output_path: str) -> bool:
    """Таймстампы слов отдаются отдельным JSON по подписанной ссылке.

    Пайплайн озвучку из-за них не роняет, но и молчать нельзя: сценарий может
    строиться вокруг таймстампов (переменная `<output>_timestamps` уходит в
    Claude), и тихий провал всплыл бы только в самом конце. Возвращаем успех,
    чтобы synthesize() показал предупреждение.
    """
    url = task.get("timestamps_url")
    if not url:
        # Нет подписки Celestial+ — сервер молча ставит include_timestamps=false.
        return False
    try:
        r = requests.get(url, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SEC)
        if r.status_code >= 400:
            return False
        base = os.path.splitext(output_path)[0]
        tmp = base + ".timestamps.json.tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, base + ".timestamps.json")
        print(f"Таймстампы сохранены → {base}.timestamps.json")
        return True
    except Exception:
        return False


def synthesize(text: str, voice: dict, output_path: str,
               cancel_check=None, skip_check=None, status_callback=None,
               label=None) -> None:
    name = label or voice.get("vg_voice_id") or voice.get("vg_template_id") or "custom"
    filename = os.path.basename(output_path) or "voice.mp3"
    print(f"Создаю задачу озвучки «{SERVICE_NAME}» (голос: {name})...")
    task = _create_task(text, voice, filename)
    task_id = task["task_id"]
    print(f"Задача «{SERVICE_NAME}» {str(task_id)[:8]}. Ожидаю...")
    done = _wait_for_task(task_id, cancel_check=cancel_check, skip_check=skip_check,
                          status_callback=status_callback)
    if status_callback:
        try:
            status_callback("Скачиваю аудио…")
        except Exception:
            pass
    print("Скачиваю аудио...")
    _download_result(task_id, output_path)
    if voice.get("vg_include_timestamps"):
        if not _download_timestamps(done, output_path):
            warn = ("Таймстампы запрошены, но сервис их не отдал (нужен тариф "
                    "Celestial или выше) — продолжаю без них")
            print(f"[{SERVICE_NAME}] {warn}")
            if status_callback:
                try:
                    status_callback(warn)
                except Exception:
                    pass
    print(f"Аудио сохранено → {output_path}")
