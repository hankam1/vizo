# YouTube AI Video Pipeline — Контекст проекта

## Цель
Автоматизация создания YouTube видео. Весь пайплайн:
YouTube URL → транскрипт → Claude (адаптация/перевод) → промпты для картинок → озвучка → картинки/видео через VeoNonStop → монтаж в CapCut вручную.

**Ключевое ограничение:** Claude используется через браузер (Playwright), НЕ через API — у пользователя подписка Claude Max x20.

---

## Режимы работы

### 1. Тартария
Берёт английское видео, адаптирует на русский язык с расширением до ~35 000 символов, локализует под российские реалии. Дополнительно генерирует ~100 промптов для архивных картинок и создаёт их через VeoNonStop (Banana, GEM_PIX_2, 16:9).

Флоу:
1. Получить заголовок видео автоматически
2. Извлечь транскрипт
3. Отправить промпт адаптации в Claude → ждать подтверждения
4. Пользователь вводит своё сообщение в UI (смотрит в браузере)
5. Ждать ~35 000 символов сценария
6. Отправить промпт для картинок + 7 примеров изображений
7. Ждать ~100 промптов
8. Параллельно: озвучка (voice API) + генерация картинок (VeoNonStop)

### 2. Перевод
Переводит видео на выбранный язык (венгерский, чешский, польский) с литературной адаптацией. Озвучка через voicer.mat3u.com.

### 3. Генерация (standalone)
Отдельный экран с 5 режимами VeoNonStop API: Text→Video, Image→Video, Component (multi-image), Batch Frame (переход), Banana Image (картинки).

---

## Структура проекта

```
automation/
├── main.py                   # CLI: меню режимов tartaria/перевод
├── config.py                 # Базовые URL + списки моделей/aspect_ratios
├── requirements.txt          # playwright, youtube-transcript-api, requests, pyperclip, aiohttp, pywebview
├── modules/
│   ├── claude_ui.py          # Playwright-автоматизация Claude
│   ├── transcript.py         # YouTube транскрипт + заголовок
│   ├── voice_api.py          # voicer.mat3u.com TTS (Bearer)
│   ├── voice_templates.py    # Пресеты голосов (tartaria, pl, hu, cs)
│   ├── veo_api.py            # VeoNonStop: картинки (Banana) + видео (5 режимов)
│   ├── api_bridge.py         # Мост pywebview → Python для UI
│   ├── settings.py           # user_settings.json
│   └── updater.py            # Автообновление
├── ui/                       # Электронный pywebview UI
│   └── index.html
├── prompts/
│   ├── tartaria/             # adaptation.txt + pictures.txt + examples/
│   └── translate/translate.txt
└── output/                   # Папки с результатами YYYY-MM-DD_HH-MM_название/
```

---

## Технические решения

### Claude через браузер (Playwright)
- Запуск реального Chrome с отдельным профилем (`chrome_profile/`) — обход Cloudflare
- Текст вставляется через буфер обмена (pyperclip + Ctrl+V) в ProseMirror редактор
- Отслеживание готовности: `document.body.innerText.length` — если не растёт 10 секунд, ответ готов
- Базовая длина фиксируется ПОСЛЕ отправки сообщения — нет ложных срабатываний
- Извлечение текста: кнопка `[data-testid="action-bar-copy"]` → буфер обмена (самый надёжный способ, т.к. `assistant-message` элементы виртуализируются при длинных чатах)

### Voice API (voicer.mat3u.com)
- Base URL: `https://voicer.mat3u.com/api/v1`
- Auth: `Authorization: Bearer <token>` header
- POST `/voice/synthesize` → poll `GET /voice/status/{task_id}` → `GET /voice/download/{task_id}`
- Финальный статус: `completed`/`done`/`success`; ошибка: `failed`/`error`; цензура: `censored`
- Пресеты в `modules/voice_templates.py`: `tartaria` (RU, eleven_v3, speed 1.1), `pl` (eleven_v3, speed 1.1), `hu` (eleven_turbo_v2_5), `cs` (eleven_turbo_v2_5)

### VeoNonStop API (картинки + видео)
- Base URL: `https://veononstop.org/api/v1`
- Auth: `X-API-Key: veo_...` header
- **Картинки** (Banana) — `POST /image/banana/generate` синхронно, 1–8 за вызов, возвращает `fifeUrl` (Google Cloud Storage signed, ~30 мин)
  - Модели: `GEM_PIX_2` (default), `NARWHAL`
  - Aspect ratios: 16:9, 9:16, 1:1, 4:3, 3:4
  - Апскейл: `/image/banana/upscale` (2K/4K, возвращает base64 JPEG)
- **Видео** — асинхронно: POST → `task_id` → `GET /video/status/{task_id}` → `GET /video/download/{task_id}`
  - Эндпоинты: `text-to-video`, `image-to-video`, `multi-image-to-video`, `batch-frame`, `upsample`
  - Длительности: 4s/6s/8s, count: 1–4, aspect: 16:9/9:16
  - Модель видео: `veo_3_1_t2v_fast_ultra_relaxed`
  - Polling каждые 10с, таймаут 30 минут
- **Concurrency** — динамически из `/account/info` (поле `concurrent_tasks`): Basic 4, Standard 12, VIP 24
- В Tartaria-пайплайне: фиксировано GEM_PIX_2 + 16:9, по 1 картинке на промпт

---

## API ключи

Ключи хранятся в `user_settings.json` (правятся через UI):
- `voice_api_key` — Bearer-токен для voicer.mat3u.com
- `veo_api_key` — `veo_...` ключ для VeoNonStop

```python
VOICE_API_BASE = "https://voicer.mat3u.com/api/v1"
VEO_BASE = "https://veononstop.org/api/v1"
```

---

## Решённые проблемы

| Проблема | Решение |
|----------|---------|
| Cloudflare блокирует Playwright | Реальный Chrome + отдельный `chrome_profile/` |
| Claude не находится (виртуализация DOM) | Кнопка copy (`action-bar-copy`) → буфер обмена |
| Ложное срабатывание "готово" до начала ответа | Фиксировать baseline длины после отправки |
| Первый ответ (подтверждение) сохраняется вместо сценария | Пользователь вводит своё сообщение вручную |
| HTML entities в заголовке (`&#39;`) | `html.unescape()` |
| Озвучка блокирует генерацию картинок | `asyncio.gather` + `run_in_executor` для синхронной функции |
| macOS: промпт уходил пустым (Ctrl+V не вставляет в Chrome) | `EDIT_MODIFIER = Meta` на darwin, `Control` иначе (`claude_ui.py`) |
| macOS: проверка «профиль Chrome занят» не работала (`SingletonLock` — висячий симлинк, `os.path.exists` его не видит) | `lexists` + проверка живости pid из симлинка; протухшие локи удаляются автоматически, ошибка только при живом Chrome |
| macOS: нет звука уведомлений (`winsound` — Windows-only) | `afplay` системных звуков (Glass/Sosumi) на darwin |

---

## В планах (отложено)

- **GUI / exe** — Nuitka-сборка (build-nuitka), без терминала. Требует пересборки после миграций
- **Авто-апскейл картинок до 2K/4K** в Tartaria-пайплайне
- **Озвучка прямо в video через VeoNonStop Component** — поле `voice` есть в API, не подключено
