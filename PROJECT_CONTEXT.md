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

### Озвучка — 4 движка за одним диспетчером
`modules/voice_api.py::synthesize(text, voice, path)` смотрит на поле `engine`
голоса и уходит в нужный модуль. Все движки принимают `cancel_check` /
`skip_check` / `status_callback` и бросают `VoiceCancelled` / `VoiceSkipped`.
Голоса лежат в `user_voices.json`, поля разных движков не пересекаются
(префиксы `vg_` и `lum_`), поэтому переключение движка ничего не затирает.

| engine | UI-имя | Base | Auth | ключ в настройках |
|---|---|---|---|---|
| `voicer` | Озвучка Матея | `voicer.mat3u.com/api/v1` | Bearer | `voice_api_key` |
| `csv666` | VoiceBot | `voiceapi.csv666.ru` | X-API-Key | `voice_api_key_csv666` |
| `voicegen` | test | `qw1voicegencore.pro` | Bearer | `voice_api_key_voicegen` |
| `lumean` | Lumean | `api.lumean.app/api/public` | X-API-KEY | `voice_api_key_lumean` |

**voicer (`voice_api.py`)** — голос из полного набора настроек.
POST `/voice/synthesize` → poll `GET /voice/status/{id}` → `GET /voice/download/{id}`.
Финальный статус: `completed`/`done`/`success`; ошибка `failed`/`error`; цензура `censored`.
Пресеты в `modules/voice_templates.py`: `tartaria` (RU, eleven_v3, speed 1.1), `pl`, `hu`, `cs`.

**csv666 (`voice_api_csv666.py`)** — голос = UUID шаблона из Telegram-бота сервиса.
POST `/tasks` → `GET /tasks/{id}/status` (до `ending`) → `GET /tasks/{id}/result`.

**voicegen / «test» (`voice_api_voicegen.py`)** — рабочее имя, заменим когда сервис
получит финальное. Внутренний код движка `voicegen` менять нельзя (записан в
голосах пользователя). Голос из настроек, но шкала целочисленная: `speed` 70–120
(100 = 1.0×), `stability`/`similarity`/`style` 0–100.
POST `/api/v1/client/tasks` → `GET /api/v1/client/tasks/{id}` (до `done`) →
`GET .../download` (может ответить 307 на S3).
- модели: `elevenLabsV3`, `elevenLabs`, `fish`, `auto`, `panda`, `starfish` — от модели
  зависит, какие параметры вообще работают (`ENGINE_PARAMS`); лишние не отправляются
- `settings_preset` (`standard`/`stable`/`expressive`/`fast`) ЗАМЕНЯЕТ ручные настройки
- `accent` только у `elevenLabsV3` — на другой модели сервис отвечает 400
- `include_timestamps` (тарифы Celestial+) — JSON кладётся рядом с mp3 как
  `.timestamps.json`; на тарифе ниже сервер молча игнорирует флаг, synthesize()
  тогда показывает предупреждение. Шаг `voice` в сценарии всегда ставит
  переменную `<output>_timestamps` (путь к JSON или пустая строка) — её
  указывают во вложениях claude_prompt, чтобы отдать таймстампы ИИ без mp3.
  Вложение-`{переменная}` с ТЕКСТОМ (ответ AI, транскрипт) тоже работает:
  раннер пишет текст в `<output_dir>/attachments/<имя>.txt` и прикрепляет
  файлом (`_resolve_attachments`); список путей прикрепляется поэлементно
- **`template_id` и настройки взаимоисключающи.** Шаблон уже содержит голос,
  модель, её настройки, пресет, разбивку, размер чанка и паузу — досылать их из
  vizo нельзя, они перебьют шаблон. При заполненном `template_id` уходят только
  `text`, `filename`, `thread_count` (+ `settings_preset`, если выбран явно:
  дока говорит, что пресет из запроса переопределяет шаблон), а в редакторе
  лишние поля скрываются

**ID шаблона.** Ни Telegram-бот test, ни веб-кабинет Lumean ID шаблона не
показывают — его отдаёт только API. Поэтому в редакторе голоса есть кнопка
«Загрузить» (`api_bridge.list_voice_templates` → `list_templates()` движка):
для test это `GET /api/v1/client/templates`, для Lumean — `GET /templates/browse`
с спуском в папки на один уровень (`GET /templates` отдал бы только корень).

**Lumean (`voice_api_lumean.py`)** — голос задаётся ТОЛЬКО шаблоном, поля `voice_id`
у заказа нет. Минимум — UUID шаблона из веб-кабинета; голос/модель/язык/настройки
точечно правятся через `config_override` на конкретный заказ (шаблон не меняется).
POST `/orders` → `GET /orders/{id}` (до `completed`) → `POST /storage/url` (путь из
`result.files[]` → временная ссылка) → GET по ссылке.
- **в `config_override` попадают только заполненные поля**: мерж идёт
  `array_replace_recursive`, явный `null` СТИРАЕТ значение шаблона
- тонкие настройки (`similarity_boost`/`style`/`use_speaker_boost`) работают только
  вместе с `advanced_voice_settings: true` — иначе молча не влияют на звук
- `generation_mode` лежит в КОРНЕ конфига, не внутри `tts_settings`
- `partially_completed` лечится автоматически: `POST /orders/{id}/items/retry-failed`
  (бесплатно, до 2 раундов). `policy_flagged` так не чинится — нужен исправленный
  текст, поэтому падаем с внятной ошибкой
- **«есть ли работа» считается по ВСЕМ записям, включая повторы** (`_order_progress`):
  сам упавший чанк остаётся `failed` и после запуска повтора, так что по одним
  чанкам повтор в полёте не виден — и retry-failed дёргался бы вхолостую, сжигая
  лимит раундов. Повторы приходят и вложенными в `retries`, и записями верхнего
  уровня с `parent_item_id` — обходим оба варианта
- если незавершённых чанков нет, а заказ всё ещё `partially_completed` — это окно
  склейки (статус заказа отстаёт от статусов чанков): ждём до `MAX_IDLE_POLLS`,
  а не объявляем провал сразу
- `generation_mode`: пусто = «как в шаблоне», явный `default` отправляется —
  иначе нельзя было бы ВЫКЛЮЧИТЬ абзацный режим, включённый в шаблоне
- **402 `payg_topup_required`** — квоты подписки не хватило. Молча не платим: доплата
  идёт только при включённом `lum_allow_payg` в голосе (409 `quote_mismatch` → берём
  свежую котировку). Выключено — понятная ошибка, деньги не списаны
- субтитры лежат отдельно, в `result.service_files[]` — качаются по флагу
  `lum_download_subtitles` рядом с mp3
- при отмене запуска шлём `POST /orders/{id}/cancel` — разблокирует средства

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

## Очередь и история запусков

- **Имя папки при запуске** — настройка `ask_folder_name` (вкл. по умолчанию): диалог запуска спрашивает название папки результата; оно видно в очереди и в имени папки. Пусто = автоимя (дата + название).
- **Заголовок ролика** — при постановке в очередь фоном подтягивается название YouTube-видео (`_fetch_video_title`, кэш по url) и показывается в карточке.
- **Дубли** — `find_duplicate(url, scenario_id|language)`: предупреждение (не запрет), если та же ссылка (сравнение по video id) уже запускалась В ТОМ ЖЕ сценарии/языке. Та же ссылка в другом пайплайне — тишина.
- **Пакетный запуск** — кнопка на экране сценариев: выбор сценария + ссылки по одной на строку, опционально `| имя папки`. Дубли сводятся в диалог «без дублей / запустить все».
- **История** — завершённые запуски сохраняются в `run_history.json` (атомарно, максимум 100) и восстанавливаются при старте; «Возобновить» работает, если args были компактны. Кнопка-корзина в очереди чистит список (файлы на диске не трогает).
- **Дозапуск с середины** — раннер сценариев пишет чекпоинт `.vizo_state.json` в папку результата после каждого батча (и удаляет при успехе). «Продолжить с места» (`resume_run`) переиспользует ту же папку: готовые батчи пропускаются, готовые mp3/картинки (`img_NNN_*`) не переделываются. Если недоигран диалог Claude — откат к его `claude_open` (чат мёртв после падения). Изменённый сценарий отвергает чекпоинт по отпечатку шагов. Translate-пайплайн возобновляется по артефактам (`script.txt` → скип Claude).

---

## Папки сценариев

- Папки — плоский список в `user_scenario_folders.json` (`{id: "f_…", name, created_at}`);
  сценарий ссылается на папку необязательным полем `folder_id` в `user_scenarios.json`.
  Старые версии приложения поле игнорируют, вложенных папок нет.
- UI: карточки папок над сценариями, клик — вход в папку (панель «Назад» + имя);
  перенос — через меню «В папку…» или drag-and-drop карточки на папку
  (и на кнопку «Назад» — чтобы убрать из папки). Поиск всегда глобальный,
  показывает плоский список с бейджем папки.
- Удаление папки сценарии не трогает — они возвращаются в общий список.
- **Обмен папками** — тот же `.vizo.json`, что у сценариев, но `kind: "folder"`:
  внутри имя папки + все сценарии с общим дедуплицированным бандлом голосов и
  файлов (`build_folder_export` / `commit_folder_import` в `api_bridge.py`).
  Импорт при совпадении имени папки предлагает «докинуть в существующую» или
  «создать новую (… (импорт))». Одиночный импорт файла сценария не изменился.

---

## API ключи

Ключи хранятся в `user_settings.json` (правятся через UI). Перед запуском UI
проверяет ключ ТОЛЬКО тех движков, что реально используются в сценарии
(`missingVoiceKey` + карта `VOICE_ENGINE_KEYS` в `ui/index.html`):
- `voice_api_key` — Bearer-токен для voicer.mat3u.com (Озвучка Матея)
- `voice_api_key_csv666` — X-API-Key для voiceapi.csv666.ru (VoiceBot)
- `voice_api_key_voicegen` — Bearer-токен для qw1voicegencore.pro (test)
- `voice_api_key_lumean` — X-API-KEY для api.lumean.app (Lumean; нужны права
  `orders.read`, `orders.write`, `orders.download`, `templates.read`)
- `veo_api_key` — `veo_...` ключ для VeoNonStop

```python
VOICE_API_BASE     = "https://voicer.mat3u.com/api/v1"
VOICE_CSV666_BASE  = "https://voiceapi.csv666.ru"
VOICE_VOICEGEN_BASE = "https://qw1voicegencore.pro"
VOICE_LUMEAN_BASE  = "https://api.lumean.app/api/public"
VEO_BASE           = "https://veononstop.org/api/v1"
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
| Сводка «размышлений» (extended thinking) попадала в результат: pill с ней рендерится ВНУТРИ `.font-claude-response`, и DOM-фолбэк извлечения захватывал её вместе с ответом (кнопка copy чистая — страдал только фолбэк) | `extractClean` в `_last_response_text`: прячет `[class*="msg-pill"]` через `display:none` на время чтения `innerText` |

---

## В планах (отложено)

- **GUI / exe** — Nuitka-сборка (build-nuitka), без терминала. Требует пересборки после миграций
- **Авто-апскейл картинок до 2K/4K** в Tartaria-пайплайне
- **Озвучка прямо в video через VeoNonStop Component** — поле `voice` есть в API, не подключено
