import json
import re
import html
import requests
from youtube_transcript_api import YouTubeTranscriptApi


class _TimeoutSession(requests.Session):
    """requests.Session с таймаутом по умолчанию.

    Раньше таймаут навешивался через socket.setdefaulttimeout() — это
    process-global состояние, которое гонялось с другими потоками GUI.
    """
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 20)
        return super().request(*args, **kwargs)


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/)([^&?/\s]{11})",
        r"^([^&?/\s]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1)
    return None


def get_title(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        match = re.search(r'<title>(.+?)\s*-\s*YouTube</title>', r.text)
        if match:
            return html.unescape(match.group(1).strip())
    except Exception:
        pass
    return ""


def download_thumbnail(url: str, output_path: str) -> bool:
    video_id = extract_video_id(url)
    if not video_id:
        return False
    for quality in ("maxresdefault", "sddefault", "hqdefault"):
        thumb_url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
        try:
            r = requests.get(thumb_url, timeout=15)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(output_path, "wb") as f:
                    f.write(r.content)
                return True
        except Exception:
            continue
    return False


def get_description(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        match = re.search(r'"shortDescription":"(.+?)","isCrawlable"', r.text)
        if match:
            raw = match.group(1)
            # Это JSON-строка — декодируем её как JSON. Старая цепочка
            # encode('latin-1') падала на не-latin-1 символах и молча
            # возвращала пустое описание.
            try:
                decoded = json.loads(f'"{raw}"', strict=False)
            except Exception:
                decoded = raw.replace("\\n", "\n").replace('\\"', '"')
            return decoded.strip()
    except Exception:
        pass
    return ""


def _clean(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def get_transcript(url: str) -> str:
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"Не удалось извлечь ID видео из: {url}")

    api = YouTubeTranscriptApi(http_client=_TimeoutSession())

    # 1) Предпочитаем английский (исходники каналов — английские).
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        return _clean(" ".join(e.text for e in fetched))
    except Exception:
        pass

    # 2) Фолбэк: ЛЮБОЙ доступный язык. Раньше здесь стоял api.fetch(video_id),
    # у которого languages по умолчанию ('en',) — то есть фолбэк повторял
    # первую попытку и не-английские видео не работали вовсе.
    last_error = None
    try:
        transcripts = list(api.list(video_id))
        manual = [t for t in transcripts if not getattr(t, "is_generated", False)]
        for t in (manual or transcripts):
            try:
                fetched = t.fetch()
                return _clean(" ".join(e.text for e in fetched))
            except Exception as e:
                last_error = e
                continue
    except Exception as e:
        last_error = e

    detail = f" ({last_error})" if last_error else ""
    raise Exception(f"Не удалось получить транскрипт для этого видео{detail}")
