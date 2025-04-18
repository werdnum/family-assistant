import argparse
import asyncio
import contextlib
import html
import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import signal
import sys
import traceback
import yaml # Import yaml
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    # PicklePersistence, # No longer needed for history
    filters,
)
import uvicorn # Import uvicorn

# Assuming processing.py contains the LLM interaction logic
from processing import get_llm_response
# Import the FastAPI app
from web_server import app as fastapi_app
# Import storage functions
from storage import (
    init_db, get_all_notes, add_message_to_history,
    get_recent_history, get_message_by_id,
    add_or_update_note # Import the function to be used as a tool
)

# --- Logging Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Constants ---
MAX_HISTORY_MESSAGES = 5 # Number of recent messages to include (excluding current)
HISTORY_MAX_AGE_HOURS = 24 # Only include messages from the last X hours

# --- Global Variables ---
application: Optional[Application] = None
ALLOWED_CHAT_IDS: list[int] = []
DEVELOPER_CHAT_ID: Optional[int] = None
PROMPTS: Dict[str, str] = {} # Global dict to hold loaded prompts
shutdown_event = asyncio.Event()

# --- Configuration Loading ---
def load_config():
    """Loads configuration from environment variables and prompts.yaml."""
    global ALLOWED_CHAT_IDS, DEVELOPER_CHAT_ID, PROMPTS
    load_dotenv()  # Load environment variables from .env file

    chat_ids_str = os.getenv("ALLOWED_CHAT_IDS", "")
    if chat_ids_str:
        try:
            ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in chat_ids_str.split(",") if cid.strip()]
            logger.info(f"Loaded {len(ALLOWED_CHAT_IDS)} allowed chat IDs.")
        except ValueError:
            logger.error("Invalid format for ALLOWED_CHAT_IDS in .env file. Should be comma-separated integers.")
            ALLOWED_CHAT_IDS = []
    else:
        logger.warning("ALLOWED_CHAT_IDS not set. Bot will respond in all chats.")
        ALLOWED_CHAT_IDS = []

    dev_chat_id_str = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id_str:
        try:
            DEVELOPER_CHAT_ID = int(dev_chat_id_str)
            logger.info(f"Developer chat ID set to {DEVELOPER_CHAT_ID}.")
        except ValueError:
            logger.error("Invalid DEVELOPER_CHAT_ID in .env file. Must be an integer.")
            DEVELOPER_CHAT_ID = None
    else:
        logger.warning("DEVELOPER_CHAT_ID not set. Error notifications will not be sent.")
        DEVELOPER_CHAT_ID = None

    # Load prompts from YAML file
    try:
        with open("prompts.yaml", "r", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                PROMPTS = loaded_prompts
                logger.info("Successfully loaded prompts from prompts.yaml")
            else:
                logger.error("Failed to load prompts: prompts.yaml is not a valid dictionary.")
                PROMPTS = {} # Reset to empty if loading fails
    except FileNotFoundError:
        logger.error("prompts.yaml not found. Using default prompt structures.")
        PROMPTS = {} # Ensure PROMPTS is initialized
    except yaml.YAMLError as e:
        logger.error(f"Error parsing prompts.yaml: {e}")
        PROMPTS = {} # Reset to empty on parsing error

# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=os.getenv("TELEGRAM_BOT_TOKEN"),
    help="Telegram Bot Token (overrides .env)",
)
parser.add_argument(
    "--openrouter-api-key",
    default=os.getenv("OPENROUTER_API_KEY"),
    help="OpenRouter API Key (overrides .env)",
)
parser.add_argument(
    "--model",
    default="openrouter/google/gemini-2.5-pro-preview-03-25",
    help="LLM model to use via OpenRouter (e.g., openrouter/google/gemini-2.5-pro-preview-03-25)",
)
args = parser.parse_args()

# --- Initial Configuration Load ---
load_config()

# --- Validate Essential Config ---
if not args.telegram_token:
    raise ValueError("Telegram Bot Token must be provided via --telegram-token or TELEGRAM_BOT_TOKEN env var")
if not args.openrouter_api_key:
    raise ValueError("OpenRouter API Key must be provided via --openrouter-api-key or OPENROUTER_API_KEY env var")

# Set OpenRouter API key for LiteLLM
os.environ["OPENROUTER_API_KEY"] = args.openrouter_api_key

# --- Helper Functions & Context Managers ---
@contextlib.asynccontextmanager
async def typing_notifications(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str = ChatAction.TYPING):
    """Context manager to send typing notifications periodically."""
    stop_event = asyncio.Event()
    async def typing_loop():
        while not stop_event.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=action)
                # Wait slightly less than the 5-second timeout of the action
                await asyncio.wait_for(stop_event.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.warning(f"Error sending chat action: {e}")
                await asyncio.sleep(5) # Avoid busy-looping on persistent errors

    typing_task = asyncio.create_task(typing_loop())
    try:
        yield
    finally:
        stop_event.set()
        # Wait briefly for the task to finish cleanly
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(typing_task, timeout=1.0)

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    chat_id = update.effective_chat.id
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Unauthorized /start command from chat_id {chat_id}")
        return
    await update.message.reply_text(f"Hello! I'm your family assistant. Your chat ID is `{chat_id}`. How can I help?")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles regular text messages and forwards them to the LLM."""
    user_message = update.message.text
    chat_id = update.effective_chat.id

    # --- Access Control ---
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Ignoring message from unauthorized chat_id {chat_id}")
        return

    logger.info(f"Received message from chat_id {chat_id}: {user_message}")

    messages: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    user_message_timestamp = update.message.date or now # Use message timestamp if available

    # Check if the message is a reply
    if update.message.reply_to_message:
        replied_msg_id = update.message.reply_to_message.message_id
        # Fetch the replied-to message from the database
        replied_db_message = await get_message_by_id(chat_id, replied_msg_id)
        if replied_db_message:
            messages.append(replied_db_message)
            logger.debug(f"Added replied-to message {replied_msg_id} to context.")
        else:
            # Fallback if not in DB (e.g., very old message) - use the text directly
            replied_message = update.message.reply_to_message
            if replied_message.from_user.id == context.bot.id:
                role = "assistant"
            else:
                role = "user"
            content = replied_message.text or replied_message.caption
            if content:
                messages.append({"role": role, "content": content})
                logger.warning(f"Replied-to message {replied_msg_id} not found in DB, using direct content.")
        # Optional: Could fetch *more* history around the replied message here
    else:
        # If not a reply, add recent history from DB
        history_messages = await get_recent_history(
            chat_id,
            limit=MAX_HISTORY_MESSAGES,
            max_age=timedelta(hours=HISTORY_MAX_AGE_HOURS)
        )
        messages.extend(history_messages)
        logger.debug(f"Added {len(history_messages)} recent messages from DB history.")

    # Add the current user message
    current_user_message_content = {"role": "user", "content": user_message}
    messages.append(current_user_message_content)

    llm_response = None
    try:
        # --- Prepare System Prompt with Notes Context ---
        system_prompt_template = PROMPTS.get("system_prompt", "{notes_context}") # Default if not found
        notes_context_str = ""
        all_notes = await get_all_notes()

        if all_notes:
            notes_list_str = ""
            note_item_format = PROMPTS.get("note_item_format", "- {title}: {content}")
            for note in all_notes:
                notes_list_str += note_item_format.format(title=note['title'], content=note['content']) + "\n"

            notes_context_header_template = PROMPTS.get("notes_context_header", "Relevant notes:\n{notes_list}")
            notes_context_str = notes_context_header_template.format(notes_list=notes_list_str.strip())
            logger.info("Prepared notes context.")

        # Insert the formatted system prompt
        final_system_prompt = system_prompt_template.format(notes_context=notes_context_str).strip()
        if final_system_prompt: # Only insert if there's content
            messages.insert(0, {"role": "system", "content": final_system_prompt})
            logger.info("Prepended system prompt to LLM messages.")

        # Send typing action using context manager
        async with typing_notifications(context, chat_id):
            # Get response from LLM via processing module (no longer passing tools)
            llm_response = await get_llm_response(messages, args.model)

        if llm_response:
            # Reply to the original message to maintain context in the Telegram chat
            # The llm_response here is the final response after potential tool calls
            await update.message.reply_text(llm_response)
        else:
            await update.message.reply_text("Sorry, I couldn't process that.")
            logger.warning("Received empty response from LLM.")

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        # Let the error_handler deal with notifying the developer
        await update.message.reply_text("Sorry, something went wrong while processing your request.")
        # Re-raise the exception so the error handler catches it
        raise
    finally:
        # --- Store messages in DB ---
        try:
            # Store user message
            await add_message_to_history(
                chat_id=chat_id,
                message_id=update.message.message_id,
                timestamp=user_message_timestamp,
                role="user",
                content=user_message
            )
            # Store bot response if successful
            if llm_response:
                # We don't have the bot's message_id easily here without sending the reply first
                # For simplicity, using a placeholder or negative ID, or could refactor to store after reply
                # Using user's message_id + 1 as a pseudo-ID for now, might collide but unlikely for context
                bot_message_pseudo_id = update.message.message_id + 1
                await add_message_to_history(
                    chat_id=chat_id,
                    message_id=bot_message_pseudo_id, # Placeholder ID
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    content=llm_response
                )
        except Exception as db_err:
            logger.error(f"Failed to store message history in DB: {db_err}", exc_info=True)
            # Optionally notify developer about DB error


async def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception,
    # but as a list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        "An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    # Send error message to developer chat if configured
    if DEVELOPER_CHAT_ID:
        # Split the message if it's too long for Telegram
        max_len = 4096
        for i in range(0, len(message), max_len):
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID, text=message[i:i + max_len], parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Failed to send error message to developer: {e}")
    else:
        logger.warning("DEVELOPER_CHAT_ID not set, cannot send error notification.")


# --- Signal Handlers ---
async def shutdown_handler(signal_name: str):
    """Initiates graceful shutdown."""
    logger.warning(f"Received signal {signal_name}. Initiating shutdown...")
    shutdown_event.set()
    # The shutdown_event.set() will trigger the cleanup in main_async
    # We just need to ensure the event is set.
    if not shutdown_event.is_set():
        shutdown_event.set()
    # The actual stopping of application/server happens in main_async's wait block

def reload_config_handler(signum, frame):
    """Handles SIGHUP for config reloading (placeholder)."""
    logger.info("Received SIGHUP signal. Reloading configuration...")
    load_config()
    # Potentially restart parts of the application if needed,
    # but be careful with state. For now, just log and reload vars.

# --- Main Application Setup & Run ---
async def main_async() -> None:
    """Initializes and runs the bot application."""
    global application
    logger.info(f"Using model: {args.model}")

    # --- Persistence Setup ---
    # Persistence is no longer used for history, but could be kept for user/bot data if needed
    # persistence = PicklePersistence(filepath="bot_persistence.pkl")

    application = (
        ApplicationBuilder()
        .token(args.telegram_token)
        # .persistence(persistence) # Removed persistence
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Register error handler
    application.add_error_handler(error_handler)

    # Initialize database schema
    await init_db()

    # Initialize application (loads persistence, etc.)
    await application.initialize()

    # Start polling and job queue (if any)
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot started and polling.")

    # --- Uvicorn Server Setup ---
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    # Run Telegram bot polling and Uvicorn server concurrently
    telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES))
    web_server_task = asyncio.create_task(server.serve())

    logger.info("Bot started polling. Web server running on http://0.0.0.0:8000")

    # Wait until shutdown signal is received
    await shutdown_event.wait()

    logger.info("Shutdown signal received. Stopping services...")

    # Stop polling first
    await application.updater.stop()
    logger.info("Telegram polling stopped.")

    # Signal Uvicorn to shut down gracefully
    server.should_exit = True
    # Wait for Uvicorn to finish
    await web_server_task
    logger.info("Web server stopped.")

    # Cancel the polling task explicitly if it hasn't finished (should have by now)
    if not telegram_task.done():
        telegram_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await telegram_task

    logger.info("All services stopped. Final shutdown.")
    # Application shutdown is handled by the signal handler

def main() -> None:
    """Sets up the event loop and signal handlers."""
    loop = asyncio.get_event_loop()

    # Setup signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig.name: asyncio.create_task(shutdown_handler(s)))

    # SIGHUP for config reload (only on Unix-like systems)
    if hasattr(signal, 'SIGHUP'):
        try:
            loop.add_signal_handler(signal.SIGHUP, reload_config_handler, signal.SIGHUP, None)
        except NotImplementedError:
            logger.warning("SIGHUP signal handler not supported on this platform.")


    try:
        logger.info("Starting application...")
        loop.run_until_complete(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.warning("Received KeyboardInterrupt/SystemExit, initiating shutdown.")
        # Ensure shutdown runs if loop was interrupted directly
        if not shutdown_event.is_set():
             loop.run_until_complete(shutdown_handler("KeyboardInterrupt/SystemExit"))
    finally:
        logger.info("Closing event loop.")
        # Cancel remaining tasks if any (should be handled by shutdown_handler mostly)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Cancelling {len(tasks)} outstanding tasks.")
            [task.cancel() for task in tasks]
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        logger.info("Application finished.")

if __name__ == "__main__":
    main()
