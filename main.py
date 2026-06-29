import asyncio
import os
import sys
from datetime import datetime

from modules.transcript import get_transcript, get_title, get_description, download_thumbnail
from modules.voice_api import synthesize
from modules.voice_templates import TRANSLATE_LANGUAGES, resolve_lang
from modules.claude_ui import ClaudeAutomation
from modules.veo_api import generate_images
from config import OUTPUT_DIR


def _safe_name(text: str, max_len: int = 50) -> str:
    return "".join(c for c in text if c.isalnum() or c in " -_").strip()[:max_len]


def create_output_dir(label: str) -> str:
    date = datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder = os.path.join(OUTPUT_DIR, f"{date}_{_safe_name(label)}")
    os.makedirs(folder, exist_ok=True)
    return folder


def select_mode() -> str:
    print("\n" + "=" * 50)
    print("   YouTube Video Pipeline")
    print("=" * 50)
    print("  1. Тартария")
    print("  2. Перевод")
    print("=" * 50)
    while True:
        choice = input("Выберите режим (1/2): ").strip()
        if choice == "1":
            return "tartaria"
        if choice == "2":
            return "translate"
        print("Введите 1 или 2.")


# ------------------------------------------------------------------
# Tartaria pipeline
# ------------------------------------------------------------------

async def run_tartaria(youtube_url: str):
    print("\nПолучаю заголовок видео...")
    title = get_title(youtube_url)
    if title:
        print(f"Заголовок: {title}")
    else:
        title = input("Не удалось получить заголовок. Введите вручную: ").strip()
    if not title:
        print("Тема не может быть пустой.")
        return

    print("\nИзвлекаю транскрипт...")
    transcript = get_transcript(youtube_url)
    print(f"Транскрипт получен — {len(transcript):,} символов")

    output_dir = create_output_dir(title)

    claude = ClaudeAutomation()
    try:
        await claude.start()
        script, image_prompts = await claude.run_tartaria(title, transcript)
    finally:
        await claude.close()

    script_path  = os.path.join(output_dir, "script.txt")
    prompts_path = os.path.join(output_dir, "image_prompts.txt")
    images_dir   = os.path.join(output_dir, "images")
    voice_path   = os.path.join(output_dir, "voiceover.mp3")

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    with open(prompts_path, "w", encoding="utf-8") as f:
        f.write(image_prompts)

    print(f"\nСценарий     → {script_path}")
    print(f"Промпты      → {prompts_path}")

    prompts_list = [p.strip() for p in image_prompts.splitlines() if p.strip()]

    print("\nЗапускаю озвучку и генерацию картинок параллельно...")

    loop = asyncio.get_event_loop()
    await asyncio.gather(
        loop.run_in_executor(None, synthesize, script, "tartaria", voice_path),
        generate_images(prompts_list, images_dir),
    )

    print(f"\n✓ Готово! Папка: {output_dir}")


# ------------------------------------------------------------------
# Translate pipeline
# ------------------------------------------------------------------

async def run_translate(youtube_url: str):
    print(f"\nДоступные языки: {', '.join(TRANSLATE_LANGUAGES)}")
    language = input("Введите язык перевода: ").strip().lower()

    try:
        preset_key = resolve_lang(language)
    except KeyError:
        print(f"Язык '{language}' не найден. Доступные: {', '.join(TRANSLATE_LANGUAGES)}")
        return

    print("\nИзвлекаю транскрипт...")
    transcript = get_transcript(youtube_url)
    print(f"Транскрипт получен — {len(transcript):,} символов")

    print("Получаю заголовок и описание видео...")
    orig_title = get_title(youtube_url)
    orig_description = get_description(youtube_url)
    print(f"Заголовок: {orig_title}")
    print(f"Описание: {len(orig_description)} символов")

    output_dir = create_output_dir(f"translate_{language}")

    claude = ClaudeAutomation()
    try:
        await claude.start()
        translated = await claude.run_translate(transcript, language)
        seo_title, seo_description = await claude.run_seo(orig_title, orig_description, language)
    finally:
        await claude.close()

    script_path    = os.path.join(output_dir, "script.txt")
    seo_path       = os.path.join(output_dir, "seo.txt")
    thumbnail_path = os.path.join(output_dir, "thumbnail.jpg")
    voice_path     = os.path.join(output_dir, "voiceover.mp3")

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(translated)
    with open(seo_path, "w", encoding="utf-8") as f:
        f.write(f"{seo_title}\n\n\n\n{seo_description}")

    print(f"\nПеревод      → {script_path}")
    print(f"SEO          → {seo_path}")

    if download_thumbnail(youtube_url, thumbnail_path):
        print(f"Превью       → {thumbnail_path}")
    else:
        print("Превью       → не удалось скачать")

    print("\nЗапускаю озвучку...")
    synthesize(translated, preset_key, voice_path)

    print(f"\n✓ Готово! Папка: {output_dir}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

async def main():
    mode = select_mode()
    youtube_url = input("Вставьте ссылку на YouTube видео: ").strip()
    if not youtube_url:
        print("Ссылка не может быть пустой.")
        return

    try:
        if mode == "tartaria":
            await run_tartaria(youtube_url)
        else:
            await run_translate(youtube_url)
    except KeyboardInterrupt:
        print("\nОтменено пользователем.")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
