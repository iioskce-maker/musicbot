import os
import asyncio
import aiohttp
from yt_dlp import YoutubeDL
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = "8682705497:AAHSE1E9GoDUd3B_ry57v4evS_nvB0-h7zA"
GENIUS_ACCESS_TOKEN = "DWrafp34xuuhmIQ2Spyaz4YHwF-QS95Xj_Frz3cCr0cLIUitfi2bQTmLQg0iw7y7"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


async def get_accurate_metadata(query):
    url = f"https://api.genius.com/search?q={query}"
    headers = {'Authorization': f'Bearer {GENIUS_ACCESS_TOKEN}'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    hits = data.get('response', {}).get('hits', [])
                    if hits:
                        best = hits[0]['result']
                        return f"{best['primary_artist']['name']} - {best['title']}"
    except:
        pass
    return query


def download_audio(query):
    for f in os.listdir(DOWNLOAD_DIR):
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, f))
        except:
            pass

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'default_search': 'ytsearch1',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([query])

            files = os.listdir(DOWNLOAD_DIR)
            if files:
                return os.path.join(DOWNLOAD_DIR, files[0])
            return None
        except Exception as e:
            print(f"Ошибка yt-dlp: {e}")
            return None


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши название трека.")
        return

    user_query = " ".join(context.args)
    status_msg = await update.message.reply_text("🔎 Уточняю название и скачиваю аудио...")

    accurate_query = await get_accurate_metadata(user_query)
    print(f"🔍 Запрос к загрузчику: {accurate_query}")

    loop = asyncio.get_event_loop()
    file_path = await loop.run_in_executor(None, download_audio, accurate_query)

    if not file_path:
        await status_msg.edit_text("❌ Не удалось найти или скачать аудио.")
        return

    await status_msg.edit_text("📤 Отправляю файл...")
    try:
        with open(file_path, "rb") as audio:
            await update.message.reply_audio(audio=audio, caption=f"✨ Найдено: {accurate_query}")

        os.remove(file_path)
        await status_msg.delete()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка при отправке: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎵 Привет! Я найду любую песню. Просто напиши /search [название]")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    print("🚀 Бот запущен (YouTube Audio Search Mode)")
    app.run_polling()


if __name__ == "__main__":
    main()