import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GENIUS_API_KEY = os.getenv("GENIUS_API_TOKEN")

if not TOKEN or not GENIUS_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GENIUS_API_TOKEN in environment variables.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Привет! Отправь мне название песни, и я найду её текст.')

async def search_song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ' '.join(context.args)
    if not query:
        await update.message.reply_text("Пожалуйста, укажите название песни после команды /search.")
        return

    try:
        headers = {'Authorization': f'Bearer {GENIUS_API_KEY}'}
        response = requests.get(
            "https://api.genius.com/search",
            params={'q': query},
            headers=headers,
            timeout=10
        )
        data = response.json()
        hits = data.get("response", {}).get("hits", [])

        if not hits:
            await update.message.reply_text("К сожалению, ничего не найдено.")
            return

        message = ""
        for hit in hits[:5]:  # Show top 5 results
            song = hit['result']
            title = song['title']
            artist = song['primary_artist']['name']
            url = song['url']
            message += f"🎵 [{title}]({url}) by {artist}\n\n"

        await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error during search: {e}")
        await update.message.reply_text("Произошла ошибка при поиске. Попробуйте позже.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search_song))

    logger.info("Bot started.")
    app.run_polling()

if __name__ == '__main__':
    main()
