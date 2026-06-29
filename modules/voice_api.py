import time
import requests
from config import VOICE_API_BASE
from modules import settings

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 30
DOWNLOAD_TIMEOUT_SEC = 600
# Сколько подряд неудачных опросов статуса терпим прежде чем падать.
# Перегруженный сервер может отвечать 5xx/429 несколько минут — задача при
# этом жива, и убивать оплаченную озвучку из-за этого нельзя.
MAX_CONSECUTIVE_POLL_FAILURES = 20


class VoiceCancelled(Exception):
    """Пользователь отменил запуск во время ожидания озвучки."""
    pass


class VoiceSkipped(Exception):
    """Пользователь пропустил шаг озвучки — сценарий продолжается без неё."""
    pass


def _headers() -> dict:
    key = settings.load().get("voice_api_key", "")
    if not key:
        raise RuntimeError("Voice API ключ не указан в настройках")
    return {"Authorization": f"Bearer {key}"}


def _create_task(text: str, preset: dict) -> dict:
    payload = {"text": text, **preset}
    r = requests.post(
        f"{VOICE_API_BASE}/voice/synthesize",
        json=payload,
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise Exception(f"Ошибка создания задачи озвучки: {r.status_code} {r.text[:300]}")
    return r.json()


def _format_status_line(data: dict) -> str:
    status = str(data.get("status", "?")).upper()
    done = data.get("completed_chunks") or data.get("processed_chunks") or data.get("current_chunk")
    total = data.get("chunks_count") or data.get("total_chunks")
    if done is not None and total:
        pct = int(done / total * 100)
        return f"[{status} {pct}% {done}/{total}]"
    progress = data.get("progress") if data.get("progress") is not None else data.get("percent")
    if progress is not None:
        pct = int(progress * 100) if progress <= 1 else int(progress)
        return f"[{status} {pct}%]"
    return f"[{status}...]"


def _wait_for_task(task_id: str, cancel_check=None, skip_check=None, status_callback=None) -> dict:
    # Без дедлайна по времени: перегруженный сервер может законно держать
    # задачу в очереди очень долго. Выйти из ожидания можно только вручную —
    # отменой запуска (cancel_check) или пропуском шага (skip_check).
    last = ""
    consecutive_failures = 0
    while True:
        if cancel_check and cancel_check():
            raise VoiceCancelled("Озвучка отменена пользователем")
        if skip_check and skip_check():
            raise VoiceSkipped(
                f"Шаг озвучки пропущен пользователем (задача {task_id} "
                "продолжит выполняться в Озвучке Матея — voicer.mat3u.com)"
            )
        try:
            r = requests.get(
                f"{VOICE_API_BASE}/voice/status/{task_id}",
                headers=_headers(),
                timeout=HTTP_TIMEOUT_SEC,
            )
            # 5xx/429 — временные проблемы сервера, опрос можно повторить.
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.RequestException(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise Exception(f"Ошибка статуса: {r.status_code} {r.text[:300]}")
            data = r.json()
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise Exception(
                    f"Опрос статуса озвучки не удался {consecutive_failures} раз подряд: {e}"
                )
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = str(data.get("status", "")).lower()

        line = _format_status_line(data)
        if line != last:
            if status_callback:
                try:
                    status_callback(line)
                except Exception:
                    pass
            print(f"  {line}    ", end="\r", flush=True)
            last = line

        if status in ("completed", "done", "success"):
            print()
            return data
        if status == "censored":
            print()
            blocked = data.get("blocked_chunks") or []
            raise Exception(f"Озвучка отклонена цензурой ElevenLabs ({len(blocked)} чанков)")
        if status in ("failed", "error", "cancelled", "canceled", "expired"):
            print()
            raise Exception(f"Ошибка генерации: {data.get('message') or data.get('error') or status}")

        time.sleep(POLL_INTERVAL_SEC)


def _download_result(task_id: str, output_path: str) -> None:
    import os
    r = requests.get(
        f"{VOICE_API_BASE}/voice/download/{task_id}",
        headers=_headers(),
        timeout=DOWNLOAD_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise Exception(f"Ошибка скачивания: {r.status_code} {r.text[:300]}")
    # Атомарная запись: оборванное скачивание не должно оставить битый mp3,
    # который потом выглядит как готовый результат.
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, output_path)


def _resolve_voice(preset_or_key) -> dict:
    """Return the FULL voice dict (with `engine` and either voicer settings or
    a csv666 `template_uuid`) for the given reference. Accepts a voice dict, a
    user-managed voice id, or an immutable built-in template key. Built-ins
    have no engine → treated as voicer."""
    if isinstance(preset_or_key, dict):
        return preset_or_key
    key = str(preset_or_key)
    try:
        from modules import voices as voice_mgr
        v = voice_mgr.get(key)
        if v:
            return v
    except Exception:
        pass
    from modules.voice_templates import get_preset
    v = dict(get_preset(key))
    v.setdefault("engine", "voicer")
    return v


def synthesize(text: str, preset_or_key, output_path: str,
               cancel_check=None, skip_check=None, status_callback=None) -> None:
    voice = _resolve_voice(preset_or_key)
    engine = str(voice.get("engine") or "voicer").lower()

    # csv666 / VoiceBot — вторая озвучка: голос задан UUID готового шаблона.
    if engine == "csv666":
        template_uuid = voice.get("template_uuid") or voice.get("uuid")
        if not template_uuid:
            raise RuntimeError("Для озвучки VoiceBot не задан UUID шаблона")
        from modules import voice_api_csv666
        voice_api_csv666.synthesize(
            text, template_uuid, output_path,
            cancel_check=cancel_check, skip_check=skip_check,
            status_callback=status_callback, label=voice.get("name"))
        return

    # voicer.mat3u.com — голос собирается из полного набора настроек.
    from modules.voices import to_api_preset
    preset = to_api_preset(voice)
    label = voice.get("name") or voice.get("id") or "custom"
    print(f"Создаю задачу озвучки (пресет: {label})...")
    created = _create_task(text, preset)
    task_id = created["task_id"]
    chunks = created.get("chunks_count", "?")
    eta = created.get("estimated_time")
    eta_str = f", ~{int(eta)}с" if eta else ""
    print(f"Задача {task_id[:8]} (чанков: {chunks}{eta_str}). Ожидаю...")
    _wait_for_task(task_id, cancel_check=cancel_check, skip_check=skip_check,
                   status_callback=status_callback)
    print("Скачиваю аудио...")
    _download_result(task_id, output_path)
    print(f"Аудио сохранено → {output_path}")
