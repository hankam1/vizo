"""Четвёртая озвучка — «Lumean» (https://api.lumean.app/api/public).

Голос у Lumean задаётся ТОЛЬКО через шаблон: поля `voice_id` у заказа не
существует. Поэтому минимум для голоса здесь — UUID шаблона (создаётся в
веб-кабинете Lumean). Всё остальное (голос, модель, язык, настройки, абзацный
режим) можно точечно переопределить на конкретный заказ через `config_override`
— шаблон при этом не меняется.

Важно про `config_override`: мерж идёт `array_replace_recursive`, и явный `null`
СТИРАЕТ значение шаблона. Поэтому в оверрайд попадают только реально
заполненные поля — пустые не отправляются вовсе.

Поток: POST /orders → GET /orders/{id} (пока не `completed`) → POST /storage/url
(путь из `result.files[]` → временная ссылка) → GET по ссылке. Авторизация —
заголовок `X-API-KEY`.
"""
import os
import time
import requests

from config import VOICE_LUMEAN_BASE
from modules import settings

# Имя движка, как его видит пользователь (в логах/ошибках/статусах).
SERVICE_NAME = "Lumean"
# Те же исключения, что и у остальных движков, чтобы пайплайн/раннер ловил их
# единообразно (отмена запуска и пропуск шага озвучки).
from modules.voice_api import VoiceCancelled, VoiceSkipped

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 30
DOWNLOAD_TIMEOUT_SEC = 600
# Сколько подряд неудачных опросов терпим до падения (см. voice_api.py).
MAX_CONSECUTIVE_POLL_FAILURES = 20
# Сколько раз пытаемся вылечить `partially_completed` массовым retry упавших
# чанков, прежде чем сдаться. Повтор бесплатен (переиспользует залоченный
# остаток), но бесконечно крутить его нельзя.
MAX_RETRY_ROUNDS = 2
# Сколько опросов ждём, если заказ ещё `partially_completed`, но незавершённых
# чанков уже нет: после повтора идёт склейка, и статус заказа отстаёт.
MAX_IDLE_POLLS = 24

# OrderStatus: created → pending → in_progress → completed. Терминальные
# неуспешные — failed / compensated / cancelled. partially_completed
# нетерминален: часть чанков упала, их можно доретраить.
DONE_STATUSES = {"completed", "result_delivered"}
ERROR_STATUSES = {"failed", "compensated", "cancelled"}
STATUS_LABELS_RU = {
    "created": "Создан",
    "pending": "В очереди",
    "in_progress": "Синтез…",
    "partially_completed": "Частично готов",
    "completed": "Готово",
    "result_delivered": "Готово",
    "failed": "Ошибка",
    "compensated": "Ошибка (средства возвращены)",
    "cancelled": "Отменён",
}

# Модели ElevenLabs, доступные в шаблоне Lumean.
MODELS = [
    "eleven_v3", "eleven_v3_beta", "eleven_multilingual_v2",
    "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_turbo_v2", "eleven_flash_v2",
]

# Понятные тексты для машинных `reason` доменных отказов заказа.
REASON_RU = {
    "insufficient_balance": "недостаточно баланса",
    "insufficient_allowance": "не хватает квоты подписки, а доплата (PAYG) у сервиса отключена",
    "no_subscription": "нет активной подписки под этот сервис",
    "entitlement_missing": "у подписки нет права на этот сервис",
    "template_access_denied": "нет доступа к шаблону",
    "limit_exceeded": "превышен лимит подписки по объёму",
    "concurrency_limit_exceeded": "исчерпаны слоты одновременных задач — дождись завершения своих заказов",
    "content_blocked": "текст уже отклонялся контентной политикой — отредактируй спорные фрагменты",
    "policy_cooldown_active": "кулдаун после серии блокировок по контентной политике",
    "token_quota_exceeded": "исчерпана токен-квота API-ключа",
    "service_not_found": "сервис не найден или не активен",
    "quote_mismatch": "цена доплаты изменилась",
    # Причины пропуска чанка в ответе retry-failed.
    "superseded": "у чанка уже есть успешная версия",
    "active_retry_exists": "повтор уже идёт",
    "max_attempts_reached": "исчерпан лимит попыток (5)",
    "insufficient_funds": "не осталось залоченного остатка",
}


def _headers() -> dict:
    key = settings.load().get("voice_api_key_lumean", "")
    if not key:
        raise RuntimeError(f"API-ключ {SERVICE_NAME} не указан в настройках")
    return {"X-API-KEY": key}


def _body(r) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _api_error(r, action: str) -> Exception:
    """Разбирает конверт ошибки Lumean в понятное сообщение."""
    body = _body(r)
    if r.status_code == 401:
        return Exception(f"Неверный или истёкший API-ключ {SERVICE_NAME}")
    if r.status_code == 403:
        return Exception(
            f"{action} {SERVICE_NAME}: нет прав у ключа или чужой ресурс "
            f"({body.get('message') or r.text[:200]})"
        )
    if r.status_code == 422 and isinstance(body.get("errors"), dict):
        # Формат Laravel: {message, errors: {field: [...]}}
        parts = [f"{k}: {'; '.join(map(str, v))}" for k, v in body["errors"].items()]
        return Exception(f"{action} {SERVICE_NAME}: {' | '.join(parts)[:400]}")
    reason = body.get("reason")
    msg = body.get("message") or r.text[:300]
    if reason:
        hint = REASON_RU.get(reason)
        retry_after = body.get("retry_after")
        tail = f" (повтор через {int(retry_after)} с)" if retry_after else ""
        return Exception(
            f"{action} {SERVICE_NAME}: {hint or reason}{tail}. {msg}"[:500]
        )
    return Exception(f"{action} {SERVICE_NAME}: {r.status_code} {msg}")


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_config_override(voice: dict) -> dict:
    """Собирает `config_override` из полей голоса (префикс `lum_`).

    В результат попадают ТОЛЬКО заполненные поля: `null`/пустая строка в
    оверрайде затёрли бы значение шаблона, а не «оставили как есть».
    """
    tts = {}
    voice_id = (voice.get("lum_voice_id") or "").strip()
    if voice_id:
        tts["voice_id"] = voice_id
    model_id = (voice.get("lum_model_id") or "").strip()
    if model_id:
        tts["model_id"] = model_id
    lang = (voice.get("lum_language_code") or "").strip()
    if lang:
        tts["language_code"] = lang

    # Тонкие настройки голоса (similarity_boost / style / use_speaker_boost)
    # работают только при advanced_voice_settings: true — без флага они молча
    # не влияют на звук.
    if voice.get("lum_override_settings"):
        src = voice.get("lum_settings") or {}
        vs = {}
        for key in ("stability", "similarity_boost", "style", "speed"):
            val = _num(src.get(key))
            if val is not None:
                vs[key] = val
        if "use_speaker_boost" in src:
            vs["use_speaker_boost"] = bool(src["use_speaker_boost"])
        if vs:
            tts["advanced_voice_settings"] = True
            tts["voice_settings"] = vs

    override = {}
    if tts:
        override["tts_settings"] = tts
    # generation_mode лежит в КОРНЕ конфига, не внутри tts_settings. Пустое
    # значение = «как в шаблоне»; явный `default` шлём, чтобы можно было
    # ВЫКЛЮЧИТЬ абзацный режим, включённый в шаблоне.
    mode = (voice.get("lum_generation_mode") or "").strip()
    if mode:
        override["generation_mode"] = mode
    return override


def list_templates() -> list:
    """Шаблоны пользователя для выбора ID в UI: `[{id, name, note}]`.

    `GET /templates` отдаёт только корень (`folder_id = null`), поэтому идём
    через `browse` и спускаемся в папки на один уровень — иначе шаблон, убранный
    в папку, в списке не появится.
    """
    def browse(folder_id=None) -> dict:
        params = {"per_page": 100}
        if folder_id is not None:
            params["folder_id"] = folder_id
        r = requests.get(
            f"{VOICE_LUMEAN_BASE}/templates/browse",
            params=params,
            headers=_headers(),
            timeout=HTTP_TIMEOUT_SEC,
        )
        if r.status_code >= 400:
            raise _api_error(r, "Ошибка загрузки шаблонов")
        return _body(r).get("data") or {}

    out, folders = [], []
    for item in (browse().get("items") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "folder":
            folders.append(item)
        elif item.get("id"):
            out.append(item)
    # Один уровень вложенности; глубже не лезем, чтобы не устроить веер запросов.
    for folder in folders[:20]:
        try:
            for item in (browse(folder.get("id")).get("items") or []):
                if isinstance(item, dict) and item.get("type") == "template" and item.get("id"):
                    item["_folder"] = folder.get("name")
                    out.append(item)
        except Exception:
            continue

    result = []
    for t in out:
        note = " · ".join(x for x in (t.get("service_key"), t.get("_folder")) if x)
        result.append({
            "id": str(t.get("id")),
            "name": str(t.get("name") or t.get("id")),
            "note": note,
        })
    return result


def _post_order(payload: dict) -> requests.Response:
    return requests.post(
        f"{VOICE_LUMEAN_BASE}/orders",
        json=payload,
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )


def _payg_error(body: dict) -> Exception:
    return Exception(
        f"{SERVICE_NAME}: не хватает квоты подписки. Требуется доплата "
        f"{body.get('shortfall_lmc')} LMC ({body.get('shortfall_tokens')} токенов). "
        f"Разреши доплату в настройках голоса или пополни квоту."
    )


def _create_order(text: str, voice: dict, name: str = None) -> str:
    template_id = (voice.get("lum_template_id") or "").strip()
    if not template_id:
        raise RuntimeError(f"Для озвучки {SERVICE_NAME} не задан ID шаблона")
    payload = {"template_id": template_id, "input_text": text}
    if name:
        payload["name"] = name[:120]
    override = build_config_override(voice)
    if override:
        payload["config_override"] = override

    r = _post_order(payload)

    # 402 payg_topup_required — квоты подписки не хватило, сервер предлагает
    # доплатить из LMC-баланса. Молча платить нельзя: это реальные деньги,
    # поэтому только при явном разрешении в настройках голоса.
    if r.status_code == 402:
        body = _body(r)
        if body.get("reason") != "payg_topup_required":
            raise _api_error(r, "Ошибка создания заказа озвучки")
        if not voice.get("lum_allow_payg"):
            raise _payg_error(body)
        for _ in range(2):
            print(f"[{SERVICE_NAME}] Доплата PAYG: {body.get('shortfall_lmc')} LMC")
            confirmed = dict(payload)
            confirmed["confirm_payg_topup"] = True
            confirmed["quote_token"] = body.get("quote_token")
            r = _post_order(confirmed)
            # 409 quote_mismatch — цена успела вырасти; берём свежую котировку.
            if r.status_code == 409:
                r = _post_order(payload)
                if r.status_code != 402:
                    break
                body = _body(r)
                continue
            break

    if r.status_code >= 400:
        raise _api_error(r, "Ошибка создания заказа озвучки")
    order = (_body(r).get("data") or {})
    order_id = order.get("id")
    if not order_id:
        raise Exception(f"{SERVICE_NAME} не вернул id заказа: {r.text[:300]}")
    return order_id


def _get_order(order_id: str) -> dict:
    r = requests.get(
        f"{VOICE_LUMEAN_BASE}/orders/{order_id}",
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    # 5xx/429 — временные проблемы сервера, опрос можно повторить.
    if r.status_code >= 500 or r.status_code == 429:
        raise requests.RequestException(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка статуса заказа")
    return _body(r).get("data") or {}


def _cancel_order(order_id: str) -> None:
    """Best-effort отмена: разблокирует средства и возвращает токен-квоту."""
    try:
        requests.post(
            f"{VOICE_LUMEAN_BASE}/orders/{order_id}/cancel",
            headers=_headers(),
            timeout=HTTP_TIMEOUT_SEC,
        )
    except Exception:
        pass


def _retry_failed(order_id: str) -> dict:
    """Массовый повтор технических `failed`-чанков. Бесплатен."""
    r = requests.post(
        f"{VOICE_LUMEAN_BASE}/orders/{order_id}/items/retry-failed",
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка повтора упавших чанков")
    return _body(r).get("data") or {}


def _status_text(order: dict) -> str:
    status = str(order.get("status", "")).lower()
    label = STATUS_LABELS_RU.get(status, status or "…")
    total = order.get("total_chunks")
    done = order.get("completed_chunks")
    if total:
        pct = order.get("progress_percent")
        if pct is None:
            pct = int((done or 0) / total * 100)
        return f"{label} {int(pct)}% {done or 0}/{total}"
    return label


ACTIVE_ITEM_STATUSES = {"pending", "processing"}


def _walk_items(order: dict):
    """Плоский обход чанков заказа вместе с их повторами.

    Повторы приходят и вложенными в `retries`, и отдельными записями верхнего
    уровня с `parent_item_id` — обходим оба варианта.
    """
    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        yield item
        for retry in item.get("retries") or []:
            if isinstance(retry, dict):
                yield retry


def _order_progress(order: dict) -> dict:
    """Что реально происходит с чанками частично готового заказа.

    `active` считается по ВСЕМ записям, включая повторы: если посмотреть только
    на исходные чанки, уже запущенный повтор не виден (сам чанк остаётся
    `failed`), и мы бы дёргали retry-failed снова и снова — сервис такие
    пропускает с `active_retry_exists`, а мы бы сожгли лимит раундов и сдались,
    хотя повтор был в полёте.

    `stuck_failed` / `flagged` — сбойные чанки, у которых нет ни успешного, ни
    идущего повтора; только они требуют действий.
    """
    items = list(_walk_items(order))
    children = {}
    for item in items:
        pid = item.get("parent_item_id")
        if pid:
            children.setdefault(str(pid), []).append(item)

    active = sum(1 for it in items
                 if str(it.get("status", "")).lower() in ACTIVE_ITEM_STATUSES)
    healthy = ACTIVE_ITEM_STATUSES | {"completed"}
    stuck_failed = flagged = 0
    for item in items:
        if item.get("item_type") not in (None, "chunk"):
            continue
        status = str(item.get("status", "")).lower()
        if status not in ("failed", "policy_flagged"):
            continue
        kids = children.get(str(item.get("id")), []) + list(item.get("retries") or [])
        if any(str(k.get("status", "")).lower() in healthy for k in kids):
            continue  # повтор уже идёт или успешно закрыл этот чанк
        if status == "failed":
            stuck_failed += 1
        else:
            flagged += 1
    return {"active": active, "stuck_failed": stuck_failed, "flagged": flagged}


def _skipped_summary(plan: dict) -> str:
    """Причины пропуска из ответа retry-failed — строим по машинному `reason`."""
    reasons = {}
    for item in plan.get("skipped") or []:
        reason = str(item.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    return ", ".join(f"{REASON_RU.get(k, k)} — {v}" for k, v in reasons.items())


def _wait_for_order(order_id: str, cancel_check=None, skip_check=None,
                    status_callback=None) -> dict:
    # Без дедлайна по времени: заказ может законно висеть в очереди долго.
    # Выйти можно только отменой запуска или пропуском шага (как у voicer).
    last = ""
    consecutive_failures = 0
    retry_rounds = 0
    idle_polls = 0
    while True:
        if cancel_check and cancel_check():
            _cancel_order(order_id)
            raise VoiceCancelled("Озвучка отменена пользователем")
        if skip_check and skip_check():
            raise VoiceSkipped(
                f"Шаг озвучки пропущен пользователем (заказ {order_id} "
                f"продолжит выполняться в {SERVICE_NAME})"
            )
        try:
            order = _get_order(order_id)
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise Exception(
                    f"Опрос статуса озвучки {SERVICE_NAME} не удался "
                    f"{consecutive_failures} раз подряд: {e}"
                )
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = str(order.get("status", "")).lower()
        line = _status_text(order)
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
            return order
        if status in ERROR_STATUSES:
            print()
            # У упавшего заказа result = null, детали ошибки наружу не отдаются.
            raise Exception(
                f"Ошибка генерации {SERVICE_NAME}: заказ {STATUS_LABELS_RU.get(status, status)}"
            )

        if status == "partially_completed":
            progress = _order_progress(order)
            # Пока хоть что-то выполняется (в том числе уже запущенный повтор) —
            # просто ждём, ничего не предпринимая.
            if progress["active"] == 0:
                failed = progress["stuck_failed"]
                flagged = progress["flagged"]
                if not failed and not flagged:
                    # Делать нечего, но заказ ещё не переключился в completed:
                    # после успешного повтора идёт склейка, и статус заказа
                    # какое-то время отстаёт от статусов чанков. Ждём, а не
                    # объявляем провал — но не бесконечно.
                    idle_polls += 1
                    if idle_polls >= MAX_IDLE_POLLS:
                        print()
                        raise Exception(
                            f"{SERVICE_NAME}: заказ завис в статусе «частично готов» "
                            f"без незавершённых чанков (заказ {order_id})"
                        )
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                idle_polls = 0
                if failed and retry_rounds < MAX_RETRY_ROUNDS:
                    print()
                    print(f"[{SERVICE_NAME}] Перезапускаю упавшие чанки: {failed}")
                    if status_callback:
                        try:
                            status_callback(f"Перезапуск упавших чанков ({failed})…")
                        except Exception:
                            pass
                    plan = _retry_failed(order_id)
                    if int(plan.get("queued_count") or 0) > 0:
                        retry_rounds += 1
                        last = ""
                        time.sleep(POLL_INTERVAL_SEC)
                        continue
                    # Ничего не встало на повтор — повторять ещё раз бессмысленно.
                    print()
                    raise Exception(
                        f"{SERVICE_NAME}: упавшие чанки перезапустить нельзя "
                        f"({_skipped_summary(plan) or 'причина не указана'}). Заказ {order_id}"
                    )
                print()
                # policy_flagged лечится только исправленным текстом чанка —
                # автоматически повторять его бессмысленно (отклонят снова).
                if flagged:
                    raise Exception(
                        f"{SERVICE_NAME}: {flagged} фрагментов отклонено контентной "
                        f"политикой. Отредактируй спорные места в тексте и запусти заново "
                        f"(заказ {order_id})"
                    )
                raise Exception(
                    f"{SERVICE_NAME}: заказ остался частично готовым, "
                    f"упавшие чанки не восстановились (заказ {order_id})"
                )

        time.sleep(POLL_INTERVAL_SEC)


def _file_paths(result: dict, key: str) -> list:
    """`files`/`service_files` — плоские массивы строк-путей."""
    out = []
    for item in (result.get(key) or []):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get("path"):
            out.append(item["path"])
    return out


def _storage_url(path: str) -> str:
    r = requests.post(
        f"{VOICE_LUMEAN_BASE}/storage/url",
        json={"path": path},
        headers=_headers(),
        timeout=HTTP_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise _api_error(r, "Ошибка получения ссылки на файл")
    url = (_body(r).get("data") or {}).get("url")
    if not url:
        raise Exception(f"{SERVICE_NAME} не вернул ссылку на файл: {r.text[:300]}")
    return url


def _download(url: str, output_path: str) -> None:
    r = requests.get(url, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SEC)
    if r.status_code >= 400:
        raise Exception(f"Ошибка скачивания {SERVICE_NAME}: {r.status_code} {r.text[:200]}")
    # Атомарная запись: оборванное скачивание не должно оставить битый mp3.
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, output_path)


def _download_subtitles(result: dict, output_path: str) -> None:
    """Субтитры/alignment лежат отдельно — в `result.service_files[]`.

    Не критично для пайплайна: молча пропускаем, если сервис их не отдал.
    """
    base = os.path.splitext(output_path)[0]
    for path in _file_paths(result, "service_files"):
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".srt", ".vtt", ".lrc"):
            continue
        try:
            _download(_storage_url(path), base + ext)
            print(f"Субтитры сохранены → {base}{ext}")
        except Exception:
            pass


def synthesize(text: str, voice: dict, output_path: str,
               cancel_check=None, skip_check=None, status_callback=None,
               label=None) -> None:
    name = label or voice.get("lum_template_id")
    print(f"Создаю заказ озвучки {SERVICE_NAME} (шаблон: {name})...")
    order_id = _create_order(text, voice, name=label)
    print(f"Заказ {SERVICE_NAME} {str(order_id)[:8]}. Ожидаю...")
    order = _wait_for_order(order_id, cancel_check=cancel_check, skip_check=skip_check,
                            status_callback=status_callback)
    result = order.get("result") or {}
    files = _file_paths(result, "files")
    if not files:
        raise Exception(f"{SERVICE_NAME}: заказ готов, но файлов в результате нет")
    if status_callback:
        try:
            status_callback("Скачиваю аудио…")
        except Exception:
            pass
    print("Скачиваю аудио...")
    # У TTS после склейки в files ровно один элемент — финальная дорожка.
    _download(_storage_url(files[0]), output_path)
    if voice.get("lum_download_subtitles"):
        _download_subtitles(result, output_path)
    print(f"Аудио сохранено → {output_path}")
