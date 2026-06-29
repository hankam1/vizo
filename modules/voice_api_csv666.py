"""Вторая озвучка — «VoiceBot» (voiceapi.csv666.ru, https://voiceapi.csv666.ru/docs).

В UI этот движок называется **VoiceBot** (первый, voicer.mat3u.com, — «Озвучка
Матея»). Внутренний код движка остаётся `csv666`.

В отличие от Озвучки Матея здесь голос НЕ собирается из настроек: задаётся UUID
готового шаблона (`template_uuid`), а сам шаблон создаётся в Telegram-боте
сервиса. Поэтому в редакторе голоса для этого движка нужен только UUID.

Поток: POST /tasks → GET /tasks/{id}/status (пока не `ending`) → GET
/tasks/{id}/result (mp3). Авторизация — заголовок `X-API-Key`.
"""
import time
import requests

from config import VOICE_CSV666_BASE
from modules import settings

# Имя движка, как его видит пользователь (в логах/ошибках/статусах).
SERVICE_NAME = "VoiceBot"
# Те же исключения, что и у voicer-движка, чтобы пайплайн/раннер ловил их
# единообразно (отмена и пропуск шага озвучки).
from modules.voice_api import VoiceCancelled, VoiceSkipped

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 30
DOWNLOAD_TIMEOUT_SEC = 600
# Сколько подряд неудачных опросов терпим до падения (см. voice_api.py).
MAX_CONSECUTIVE_POLL_FAILURES = 20
# Если result отвечает 202 «ещё не готово» — сколько раз ждём перед сдачей.
MAX_RESULT_RETRIES = 60

# OrderStatus из API: waiting → processing → ending → ending_processed.
# error / error_handled — терминальные неуспешные.
DONE_STATUSES = {"ending", "ending_processed"}
ERROR_STATUSES = {"error", "error_handled"}
# Запасной русский перевод enum'а на случай, если API не вернул status_label.
STATUS_LABELS_RU = {
    "waiting": "В очереди",
    "processing": "Синтез…",
    "ending": "Готово, скачиваю",
    "ending_processed": "Готово",
    "error": "Ошибка",
    "error_handled": "Ошибка (средства возвращены)",
}


def _headers() -> dict:
    key = settings.load().get("voice_api_key_csv666", "")
    if not key:
        raise RuntimeError(f"API-ключ {SERVICE_NAME} не указан в настройках")
    return {"X-API-Key": key}


def _create_task(text: str, template_uuid: str) -> int:
    payload = {"text": text, "template_uuid": template_uuid}
    r = requests.post(
        f"{VOICE_CSV666_BASE}/tasks",
        json=payload,
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code == 402:
        raise Exception(f"Недостаточно средств на балансе {SERVICE_NAME}")
    if r.status_code == 429:
        raise Exception(f"Превышен лимит одновременных задач {SERVICE_NAME} (до 5)")
    if r.status_code >= 400:
        raise Exception(f"Ошибка создания задачи озвучки {SERVICE_NAME}: {r.status_code} {r.text[:300]}")
    data = r.json()
    task_id = data.get("task_id")
    if task_id is None:
        raise Exception(f"{SERVICE_NAME} не вернул task_id: {r.text[:300]}")
    return task_id


def _error_message(err) -> str:
    """TaskError приходит объектом {code, en, ru}; берём русское сообщение."""
    if isinstance(err, dict):
        return err.get("ru") or err.get("en") or err.get("code") or "неизвестная ошибка"
    return str(err) if err else "неизвестная ошибка"


def _status_text(data: dict) -> str:
    status = str(data.get("status", "")).lower()
    # status_label сервис отдаёт уже на русском («Готов», «В очереди»…) —
    # пишем именно его; иначе переводим enum сами.
    return data.get("status_label") or STATUS_LABELS_RU.get(status, status or "…")


def _wait_for_task(task_id, cancel_check=None, skip_check=None, status_callback=None) -> dict:
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
                f"продолжит выполняться в {SERVICE_NAME})"
            )
        try:
            r = requests.get(
                f"{VOICE_CSV666_BASE}/tasks/{task_id}/status",
                headers=_headers(),
                timeout=HTTP_TIMEOUT_SEC,
            )
            # 5xx/429 — временные проблемы сервера, опрос можно повторить.
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.RequestException(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise Exception(f"Ошибка статуса {SERVICE_NAME}: {r.status_code} {r.text[:300]}")
            data = r.json()
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise Exception(
                    f"Опрос статуса озвучки {SERVICE_NAME} не удался {consecutive_failures} раз подряд: {e}"
                )
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = str(data.get("status", "")).lower()
        line = _status_text(data)
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
            return data
        if status in ERROR_STATUSES:
            print()
            raise Exception(f"Ошибка генерации {SERVICE_NAME}: {_error_message(data.get('error'))}")

        time.sleep(POLL_INTERVAL_SEC)


def _download_result(task_id, output_path, cancel_check=None) -> None:
    import os
    retries = 0
    while True:
        if cancel_check and cancel_check():
            raise VoiceCancelled("Озвучка отменена пользователем")
        r = requests.get(
            f"{VOICE_CSV666_BASE}/tasks/{task_id}/result",
            headers=_headers(),
            timeout=DOWNLOAD_TIMEOUT_SEC,
        )
        # 202 — результат ещё не готов; статус уже был ending, но файл собирается.
        if r.status_code == 202:
            retries += 1
            if retries >= MAX_RESULT_RETRIES:
                raise Exception(f"{SERVICE_NAME} так и не отдал готовый файл (202)")
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if r.status_code >= 400:
            raise Exception(f"Ошибка скачивания {SERVICE_NAME}: {r.status_code} {r.text[:300]}")
        # Атомарная запись: оборванное скачивание не должно оставить битый mp3.
        tmp = output_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, output_path)
        return


def synthesize(text: str, template_uuid: str, output_path: str,
               cancel_check=None, skip_check=None, status_callback=None,
               label=None) -> None:
    name = label or template_uuid
    print(f"Создаю задачу озвучки {SERVICE_NAME} (шаблон: {name})...")
    task_id = _create_task(text, template_uuid)
    print(f"Задача {SERVICE_NAME} #{task_id}. Ожидаю...")
    _wait_for_task(task_id, cancel_check=cancel_check, skip_check=skip_check,
                   status_callback=status_callback)
    if status_callback:
        try:
            status_callback("Скачиваю аудио…")
        except Exception:
            pass
    print("Скачиваю аудио...")
    _download_result(task_id, output_path, cancel_check=cancel_check)
    print(f"Аудио сохранено → {output_path}")
