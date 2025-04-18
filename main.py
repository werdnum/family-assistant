import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Assuming processing.py contains the LLM interaction logic
from processing import get_llm_response

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Environment and Argument Parsing ---

load_dotenv()  # Load environment variables from .env file

parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=os.getenv("TELEGRAM_BOT_TOKEN"),
    help="Telegram Bot Token",
)
parser.add_argument(
    "--openrouter-api-key",
    default=os.getenv("OPENROUTER_API_KEY"),
    help="OpenRouter API Key",
)
parser.add_argument(
    "--model",
    default="openrouter/google/gemini-2.5-pro-preview-03-25",
    help="LLM model to use via OpenRouter (e.g., openrouter/google/gemini-2.5-pro-preview-03-25)",
)

args = parser.parse_args()

if not args.telegram_token:
    raise ValueError("Telegram Bot Token must be provided via --telegram-token or TELEGRAM_BOT_TOKEN env var")
if not args.openrouter_api_key:
    raise ValueError("OpenRouter API Key must be provided via --openrouter-api-key or OPENROUTER_API_KEY env var")

# Set OpenRouter API key for LiteLLM
os.environ["OPENROUTER_API_KEY"] = args.openrouter_api_key

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text("Hello! I'm your family assistant. How can I help?")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles regular text messages and forwards them to the LLM."""
    user_message = update.message.text
    chat_id = update.effective_chat.id
    logger.info(f"Received message from chat_id {chat_id}: {user_message}")

    try:
        # Send typing action
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Get response from LLM via processing module
        llm_response = await get_llm_response(user_message, args.model)

        if llm_response:
            await update.message.reply_text(llm_response)
        else:
            await update.message.reply_text("Sorry, I couldn't process that.")
            logger.warning("Received empty response from LLM.")

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        await update.message.reply_text("Sorry, something went wrong while processing your request.")

# --- Main Application Setup ---

def main() -> None:
    """Start the bot."""
    logger.info(f"Using model: {args.model}")

    application = Application.builder().token(args.telegram_token).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
