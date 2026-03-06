import os
import subprocess
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# вставь сюда свой токен бота
TOKEN = "8682705497:AAHSE1E9GoDUd3B_ry57v4evS_nvB0-h7zA"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def clear_downloads():
    for f in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(path):
            os.remove(path)


def download_track(query):

    clear_downloads()

    cmd = [
        "scdl",
        "-s", query,
        "--path", DOWNLOAD_DIR,
        "--onlymp3",
        "--no-playlist"
    ]

    subprocess.run(cmd)

    files = os.listdir(DOWNLOAD_DIR)

    if not files:
        return None

    return os.path.join(DOWNLOAD_DIR, files[0])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 SoundCloud MP3 бот\n\n"
        "Использование:\n"
        "/search название трека"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text("Использование: /search название трека")
        return

    query = " ".join(context.args)

    await update.message.reply_text("🔎 Ищу и скачиваю трек...")

    file_path = download_track(query)

    if not file_path:
        await update.message.reply_text("❌ Трек не найден")
        return

    await update.message.reply_audio(
        audio=open(file_path, "rb")
    )

    os.remove(file_path)


def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))

    print("Bot started")

    app.run_polling()


if __name__ == "__main__":
    main()