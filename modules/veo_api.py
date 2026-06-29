"""Клиент VeoNonStop API — генерация картинок и видео.

База: https://veononstop.org/api/v1
Авторизация: X-API-Key с префиксом 'veo_'.

Картинки (Banana) — синхронные, видео — асинхронные с polling.
"""

import asyncio
import base64
import json as _json
import os
import time
from typing import Callable, Optional

import aiohttp
import requests

from config import VEO_BASE
from modules import settings


# ---------- общие константы ----------

DEFAULT_IMAGE_MODEL = "GEM_PIX_2"
DEFAULT_IMAGE_ASPECT = "16:9"
VIDEO_POLL_INTERVAL_SEC = 10
VIDEO_TIMEOUT_SEC = 1800  # 30 минут — лимит сервиса
# Сколько подряд неудачных опросов статуса терпим. VeoNonStop при сбоях своей
# инфраструктуры отдаёт 503 ("API key validation service unavailable")
# по несколько минут — оплаченный рендер из-за этого убивать нельзя.
MAX_POLL_FAILURES = 20


def _headers() -> dict:
    key = settings.load().get("veo_api_key", "")
    if not key:
        raise RuntimeError("VeoNonStop API ключ не указан в настройках")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _check(data: dict, label: str) -> dict:
    if not data.get("success"):
        raise Exception(f"{label}: {data.get('error', 'unknown error')}")
    if "data" not in data:
        raise Exception(f"{label}: ответ без поля 'data'")
    return data["data"]


# ---------- account ----------

def get_account_info() -> dict:
    r = requests.get(f"{VEO_BASE}/account/info", headers=_headers(), timeout=30)
    r.raise_for_status()
    return _check(r.json(), "account/info")


def get_account_usage() -> dict:
    r = requests.get(f"{VEO_BASE}/account/usage", headers=_headers(), timeout=30)
    r.raise_for_status()
    return _check(r.json(), "account/usage")


def get_concurrency_limit(default: int = 4) -> int:
    """Лимит одновременных задач из плана пользователя.

    Клэмп к >=1 обязателен: concurrent_tasks=0 (истёкший план) превращается
    в Semaphore(0) — все задачи вечно ждут слот и пайплайн зависает молча.
    """
    try:
        info = get_account_info()
        return max(1, int(info.get("concurrent_tasks", default)))
    except Exception:
        return max(1, default)


# ---------- картинки (Banana) ----------

async def _banana_post(
    session: aiohttp.ClientSession,
    prompt: str,
    num_images: int,
    model_key: str,
    aspect_ratio: str,
    reference_images: Optional[list[dict]],
    use_all_ref_images: bool,
    project_id: Optional[str],
) -> dict:
    payload = {
        "prompt": prompt,
        "num_images": num_images,
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
    }
    if reference_images:
        payload["reference_images"] = reference_images
        payload["use_all_ref_images"] = use_all_ref_images
    if project_id:
        payload["project_id"] = project_id

    async with session.post(
        f"{VEO_BASE}/image/banana/generate",
        json=payload,
        headers=_headers(),
    ) as r:
        text = await r.text()
        if r.status >= 400:
            raise Exception(f"banana/generate {r.status}: {text[:300]}")
        return _check(_json.loads(text), "banana/generate")


async def _download_url(session: aiohttp.ClientSession, url: str, output_path: str) -> None:
    async with session.get(url) as r:
        r.raise_for_status()
        content = await r.read()
    # Атомарно: оборванное скачивание не должно оставить частичный файл,
    # который потом будет принят за готовый (skip-existing при повторе).
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, output_path)


async def _generate_one_image(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    prompt: str,
    output_path: str,
    model_key: str,
    aspect_ratio: str,
    index: int,
    total: int,
) -> None:
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        print(f"  [{index}/{total}] ↩ уже есть, пропускаю")
        return
    async with sem:
        for attempt in range(3):
            try:
                data = await _banana_post(
                    session, prompt,
                    num_images=1,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                    reference_images=None,
                    use_all_ref_images=False,
                    project_id=None,
                )
                media = data.get("media") or []
                if not media:
                    raise RuntimeError("пустой media[]")
                await _download_url(session, media[0]["fifeUrl"], output_path)
                print(f"  [{index}/{total}] ✓ {os.path.basename(output_path)}")
                return
            except Exception as e:
                if attempt < 2:
                    print(f"  [{index}/{total}] ✗ {e} — повтор через 10с...")
                    await asyncio.sleep(10)
                else:
                    raise


async def generate_images(
    prompts: list[str],
    output_dir: str,
    model_key: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = DEFAULT_IMAGE_ASPECT,
    concurrency: Optional[int] = None,
) -> None:
    """Генерация одной картинки на промпт, как было в Tartaria-пайплайне.

    concurrency=None → читается из /account/info (план пользователя).
    """
    if concurrency is None:
        # Блокирующий requests-вызов — уводим в executor, чтобы не стопорить
        # event loop (рядом в gather может крутиться озвучка).
        loop = asyncio.get_event_loop()
        concurrency = await loop.run_in_executor(None, get_concurrency_limit, 4)
    concurrency = max(1, int(concurrency))
    os.makedirs(output_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            _generate_one_image(
                session, sem, prompt,
                os.path.join(output_dir, f"image_{i:03d}.png"),
                model_key, aspect_ratio,
                i, len(prompts),
            )
            for i, prompt in enumerate(prompts, 1)
        ]
        # return_exceptions: одна неудачная картинка не должна обрывать весь
        # батч и закрывать сессию под ногами у остальных скачиваний.
        results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [(i, r) for i, r in enumerate(results, 1) if isinstance(r, Exception)]
    if failures:
        idx_list = ", ".join(str(i) for i, _ in failures[:10])
        raise RuntimeError(
            f"{len(failures)} из {len(prompts)} картинок не сгенерировались "
            f"(промпты №{idx_list}). Первая ошибка: {failures[0][1]}. "
            "Готовые картинки сохранены — повторный запуск догенерирует недостающие."
        )
    print(f"\nВсе изображения сохранены → {output_dir}")


async def banana_generate_batch(
    prompt: str,
    num_images: int = 1,
    model_key: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = DEFAULT_IMAGE_ASPECT,
    reference_images: Optional[list[dict]] = None,
    use_all_ref_images: bool = False,
    project_id: Optional[str] = None,
) -> dict:
    """Один вызов banana/generate, возвращает data с media[]."""
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _banana_post(
            session, prompt, num_images, model_key, aspect_ratio,
            reference_images, use_all_ref_images, project_id,
        )


# Перманентные отказы Google (safety-фильтр, невалидный промпт) — повтор их
# не спасёт, в отличие от транзиентных сбоев инфраструктуры VeoNonStop
# ("All cookie slots full", reCAPTCHA-таймаут, 500/503/504).
_PERMANENT_MARKERS = (
    "INVALID_ARGUMENT", "BAD_REQUEST", "safety", "SAFETY",
    "blocked", "PROHIBITED",
)


def _is_permanent_error(err: Exception) -> bool:
    msg = str(err)
    return any(m in msg for m in _PERMANENT_MARKERS)


async def banana_generate_with_retry(
    prompt: str,
    num_images: int = 1,
    model_key: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = DEFAULT_IMAGE_ASPECT,
    reference_images: Optional[list[dict]] = None,
    use_all_ref_images: bool = False,
    project_id: Optional[str] = None,
    attempts: int = 4,
) -> dict:
    """banana_generate_batch с повтором транзиентных сбоев.

    Сервис VeoNonStop регулярно отдаёт 500 "All cookie slots full" /
    reCAPTCHA-таймауты — это временная перегрузка, на повторе обычно проходит.
    Без ретрая одиночная генерация (banana_one / standalone-панель) падала на
    первом же таком сбое, тогда как batch-путь их переживал. Перманентные
    отказы Google (safety/INVALID_ARGUMENT) не ретраим — это пустая трата
    времени. Backoff: 5с, 10с, 20с между попытками.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await banana_generate_batch(
                prompt=prompt,
                num_images=num_images,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                reference_images=reference_images,
                use_all_ref_images=use_all_ref_images,
                project_id=project_id,
            )
        except Exception as e:
            if _is_permanent_error(e) or attempt >= attempts:
                raise
            await asyncio.sleep((5, 10, 20)[min(attempt - 1, 2)])


def upscale_image(
    media_id: str,
    project_id: str,
    target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_2K",
) -> bytes:
    """Возвращает декодированные bytes JPEG."""
    payload = {
        "media_id": media_id,
        "project_id": project_id,
        "target_resolution": target_resolution,
    }
    r = requests.post(
        f"{VEO_BASE}/image/banana/upscale",
        json=payload,
        headers=_headers(),
        timeout=300,
    )
    if r.status_code >= 400:
        raise Exception(f"banana/upscale {r.status_code}: {r.text[:300]}")
    data = _check(r.json(), "banana/upscale")
    return base64.b64decode(data["encodedImage"])


# ---------- видео ----------

def _post_video(endpoint: str, payload: dict) -> str:
    # 5xx/429 на создании задачи — временные (кредиты ещё не потрачены),
    # поэтому пробуем трижды с паузой, прежде чем ронять шаг сценария.
    for attempt in range(3):
        r = requests.post(
            f"{VEO_BASE}{endpoint}",
            json=payload,
            headers=_headers(),
            timeout=60,
        )
        if (r.status_code >= 500 or r.status_code == 429) and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise Exception(f"{endpoint} {r.status_code}: {r.text[:300]}")
        result = _check(r.json(), endpoint)
        task_id = result.get("task_id")
        if not task_id:
            raise Exception(f"{endpoint}: ответ без task_id")
        return task_id


def _norm_duration(duration: Optional[str]) -> Optional[str]:
    """API принимает только '4s' и '6s'; 8 секунд — умолчание при ОТСУТСТВИИ
    параметра. Явное '8s' (исторический дефолт в наших сценариях/UI)
    превращаем в omission, иначе сервер ответит 400."""
    if not duration or str(duration).strip().lower() in ("8s", "8"):
        return None
    return duration


def text_to_video(
    prompt: str,
    aspect_ratio: str = "16:9",
    count: int = 1,
    duration: Optional[str] = None,
) -> str:
    payload = {"prompt": prompt, "aspect_ratio": aspect_ratio, "count": count}
    duration = _norm_duration(duration)
    if duration:
        payload["duration"] = duration
    return _post_video("/video/text-to-video", payload)


def image_to_video(
    prompt: str,
    image_base64: str,
    mime_type: str = "image/jpeg",
    aspect_ratio: str = "9:16",
    count: int = 1,
    duration: Optional[str] = None,
) -> str:
    payload = {
        "prompt": prompt,
        "image_base64": image_base64,
        "mime_type": mime_type,
        "aspect_ratio": aspect_ratio,
        "count": count,
    }
    duration = _norm_duration(duration)
    if duration:
        payload["duration"] = duration
    return _post_video("/video/image-to-video", payload)


def multi_image_to_video(
    prompt: str,
    images: list[dict],
    aspect_ratio: str = "16:9",
    count: int = 1,
) -> str:
    """images — список объектов {name, image_base64, mime_type}."""
    payload = {
        "prompt": prompt,
        "images": images,
        "aspect_ratio": aspect_ratio,
        "count": count,
    }
    return _post_video("/video/multi-image-to-video", payload)


def batch_frame(
    prompt: str,
    start_image_base64: str,
    end_image_base64: str,
    aspect_ratio: str = "16:9",
    count: int = 1,
) -> str:
    payload = {
        "prompt": prompt,
        "start_image_base64": start_image_base64,
        "end_image_base64": end_image_base64,
        "aspect_ratio": aspect_ratio,
        "count": count,
    }
    return _post_video("/video/batch-frame", payload)


def upsample_video(
    media_generation_id: str,
    aspect_ratio: str = "16:9",
    video_url: Optional[str] = None,
) -> str:
    payload = {
        "media_generation_id": media_generation_id,
        "aspect_ratio": aspect_ratio,
    }
    if video_url:
        payload["video_url"] = video_url
    return _post_video("/video/upsample", payload)


def get_video_status(task_id: str) -> dict:
    r = requests.get(
        f"{VEO_BASE}/video/status/{task_id}",
        headers=_headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise Exception(f"video/status {r.status_code}: {r.text[:300]}")
    return _check(r.json(), "video/status")


def wait_for_video(
    task_id: str,
    on_progress: Optional[Callable[[dict], None]] = None,
    timeout: int = VIDEO_TIMEOUT_SEC,
) -> dict:
    deadline = time.time() + timeout
    poll_failures = 0
    while time.time() < deadline:
        # Временные сбои сети/сервера не должны убивать 30-минутный оплаченный
        # рендер — терпим до MAX_POLL_FAILURES неудачных опросов подряд.
        try:
            data = get_video_status(task_id)
            poll_failures = 0
        except Exception:
            poll_failures += 1
            if poll_failures >= MAX_POLL_FAILURES:
                raise
            time.sleep(VIDEO_POLL_INTERVAL_SEC)
            continue
        if on_progress:
            on_progress(data)
        status = str(data.get("status", "")).lower()
        if status == "completed":
            return data
        if status == "failed":
            raise Exception(f"video failed: {data.get('error', 'unknown')}")
        if status in ("cancelled", "canceled"):
            raise Exception(f"video cancelled: {task_id}")
        time.sleep(VIDEO_POLL_INTERVAL_SEC)
    raise TimeoutError(f"Видео {task_id} не завершилось за {timeout}с")


def download_video(task_id: str, output_path: str, video_index: int = 0) -> None:
    r = requests.get(
        f"{VEO_BASE}/video/download/{task_id}",
        headers=_headers(),
        params={"video_index": video_index},
        timeout=600,
        stream=True,
    )
    if r.status_code >= 400:
        raise Exception(f"video/download {r.status_code}: {r.text[:300]}")
    # Стримим во временный файл и атомарно переименовываем: обрыв на середине
    # не должен оставить частичный .mp4, который посчитают готовым видео.
    tmp = output_path + ".tmp"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    os.replace(tmp, output_path)


def cancel_task(task_id: str) -> dict:
    r = requests.post(
        f"{VEO_BASE}/video/cancel/{task_id}",
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return _check(r.json(), "video/cancel")


def cancel_all() -> dict:
    r = requests.post(
        f"{VEO_BASE}/video/cancel-all",
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return _check(r.json(), "video/cancel-all")


# ---------- утилиты для UI ----------

def file_to_base64(path: str) -> tuple[str, str]:
    """Читает картинку и возвращает (base64, mime_type)."""
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return b64, mime
