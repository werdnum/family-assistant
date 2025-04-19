import argparse
import asyncio
import contextlib
import html
import argparse
import argparse
import asyncio
import base64 # Add base64
import contextlib
import html
import io # Add io
import json
import logging
import os
import signal
import sys
import traceback
import yaml
import mcp  # Import MCP
from mcp import ClientSession, StdioServerParameters  # MCP specifics
from mcp.client.stdio import stdio_client  # MCP stdio client
from contextlib import AsyncExitStack  # For managing multiple async contexts
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple  # Added Tuple

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
import telegramify_markdown # Import the new library
from telegram.helpers import escape_markdown
import uvicorn  # Import uvicorn

# Assuming processing.py contains the LLM interaction logic
from processing import get_llm_response

# Import the FastAPI app
from web_server import app as fastapi_app

# Import storage functions
from storage import (
    init_db,
    get_all_notes,
    add_message_to_history,
    get_recent_history,
    get_message_by_id,
    add_or_update_note,  # Import the function to be used as a tool
)

# Import calendar functions
import calendar_integration

# --- Logging Configuration ---
# Set root logger level back to INFO
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Keep external libraries less verbose unless needed
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.INFO)
logging.getLogger("caldav").setLevel(
    logging.INFO
)  # Keep caldav at INFO unless specific issues arise
logger = logging.getLogger(__name__)

# --- Constants ---
MAX_HISTORY_MESSAGES = 5  # Number of recent messages to include (excluding current)
HISTORY_MAX_AGE_HOURS = 24  # Only include messages from the last X hours

# --- Global Variables ---
application: Optional[Application] = None
ALLOWED_CHAT_IDS: list[int] = []
DEVELOPER_CHAT_ID: Optional[int] = None
PROMPTS: Dict[str, str] = {}  # Global dict to hold loaded prompts
CALENDAR_CONFIG: Dict[str, Any] = {}  # Stores CalDAV and iCal settings
shutdown_event = asyncio.Event()
mcp_sessions: Dict[str, ClientSession] = (
    {}
)  # Stores active MCP client sessions {server_id: session}
mcp_tools: List[Dict[str, Any]] = []  # Stores discovered MCP tools in OpenAI format
tool_name_to_server_id: Dict[str, str] = {}  # Maps MCP tool names to their server_id
mcp_exit_stack = AsyncExitStack()  # Manages MCP server process lifecycles


# --- Configuration Loading ---
def load_config():
    """Loads configuration from environment variables and prompts.yaml."""
    global ALLOWED_CHAT_IDS, DEVELOPER_CHAT_ID, PROMPTS, CALENDAR_CONFIG # Renamed global
    load_dotenv()  # Load environment variables from .env file

    # --- Telegram Config ---
    chat_ids_str = os.getenv("ALLOWED_CHAT_IDS", "")
    if chat_ids_str:
        try:
            ALLOWED_CHAT_IDS = [
                int(cid.strip()) for cid in chat_ids_str.split(",") if cid.strip()
            ]
            logger.info(f"Loaded {len(ALLOWED_CHAT_IDS)} allowed chat IDs.")
        except ValueError:
            logger.error(
                "Invalid format for ALLOWED_CHAT_IDS in .env file. Should be comma-separated integers."
            )
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
        logger.warning(
            "DEVELOPER_CHAT_ID not set. Error notifications will not be sent."
        )
        DEVELOPER_CHAT_ID = None

    # Load prompts from YAML file
    try:
        with open("prompts.yaml", "r", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                PROMPTS = loaded_prompts
                logger.info("Successfully loaded prompts from prompts.yaml")
            else:
                logger.error(
                    "Failed to load prompts: prompts.yaml is not a valid dictionary."
                )
                PROMPTS = {}  # Reset to empty if loading fails
    except FileNotFoundError:
        logger.error("prompts.yaml not found. Using default prompt structures.")
        PROMPTS = {}  # Ensure PROMPTS is initialized
    except yaml.YAMLError as e:
        logger.error(f"Error parsing prompts.yaml: {e}")
        PROMPTS = {}  # Reset to empty on parsing error

    # --- Calendar Config (CalDAV & iCal) ---
    CALENDAR_CONFIG = {} # Initialize the combined config dict
    caldav_enabled = False
    ical_enabled = False

    # CalDAV settings
    caldav_user = os.getenv("CALDAV_USERNAME")
    caldav_pass = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str = os.getenv("CALDAV_CALENDAR_URLS")
    caldav_urls = [url.strip() for url in caldav_urls_str.split(',')] if caldav_urls_str else []

    if caldav_user and caldav_pass and caldav_urls:
        CALENDAR_CONFIG["caldav"] = {
            "username": caldav_user,
            "password": caldav_pass,
            "calendar_urls": caldav_urls,
        }
        caldav_enabled = True
        logger.info(f"Loaded CalDAV configuration for {len(caldav_urls)} specific calendar URL(s).")
    else:
        logger.info("CalDAV configuration incomplete or disabled (requires USERNAME, PASSWORD, CALENDAR_URLS).")

    # iCal settings
    ical_urls_str = os.getenv("ICAL_URLS")
    ical_urls = [url.strip() for url in ical_urls_str.split(',')] if ical_urls_str else []

    if ical_urls:
        CALENDAR_CONFIG["ical"] = {
            "urls": ical_urls,
        }
        ical_enabled = True
        logger.info(f"Loaded iCal configuration for {len(ical_urls)} URL(s).")
    else:
        logger.info("iCal configuration incomplete or disabled (requires ICAL_URLS).")

    if not caldav_enabled and not ical_enabled:
        logger.warning("No calendar sources (CalDAV or iCal) are configured. Calendar features will be disabled.")
        CALENDAR_CONFIG = {} # Ensure it's empty if nothing is enabled


# --- MCP Configuration Loading & Connection ---
async def load_mcp_config_and_connect():
    """Loads MCP server config, connects to servers, and discovers tools."""
    global mcp_sessions, mcp_tools, tool_name_to_server_id, mcp_exit_stack
    config_path = "mcp_config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.info(f"{config_path} not found. Skipping MCP server connections.")
        return
    except json.JSONDecodeError as e:
        logger.error(
            f"Error decoding {config_path}: {e}. Skipping MCP server connections."
        )
        return

    mcp_server_configs = config.get("mcpServers", {})
    if not mcp_server_configs:
        logger.info("No servers defined in mcpServers section of mcp_config.json.")
        return

    logger.info(f"Found {len(mcp_server_configs)} MCP server configurations.")

    async def _connect_and_discover_mcp(server_id: str, server_conf: Dict[str, Any]) -> Tuple[Optional[ClientSession], List[Dict[str, Any]], Dict[str, str]]:
        """Connects to a single MCP server, discovers tools, and returns results."""
        discovered_tools = []
        tool_map = {}
        session = None

        command = server_conf.get("command")
        args = server_conf.get("args", [])
        env_config = server_conf.get("env")  # Original env config from JSON

        # --- Resolve environment variable placeholders ---
        resolved_env = None
        if isinstance(env_config, dict):
            resolved_env = {}
            for key, value in env_config.items():
                if isinstance(value, str) and value.startswith("$"):
                    env_var_name = value[1:]  # Remove the leading '$'
                    resolved_value = os.getenv(env_var_name)
                    if resolved_value is not None:
                        resolved_env[key] = resolved_value
                        logger.debug(
                            f"Resolved env var '{env_var_name}' for MCP server '{server_id}'"
                        )
                    else:
                        logger.warning(
                            f"Env var '{env_var_name}' for MCP server '{server_id}' not found in environment. Omitting."
                        )
                        # Optionally, keep the placeholder or raise an error
                        # resolved_env[key] = value # Keep placeholder if preferred
                else:
                    # Keep non-placeholder values as is
                    resolved_env[key] = value
        elif env_config is not None:
            logger.warning(
                f"MCP server '{server_id}' has non-dictionary 'env' configuration. Ignoring."
            )
        # --- End environment variable resolution ---

        if not command:
            logger.error(f"MCP server '{server_id}': 'command' is missing.")
            return None, [], {}

        logger.info(f"Attempting connection and discovery for MCP server '{server_id}'...")
        try:
            server_params = StdioServerParameters(command=command, args=args, env=resolved_env)
            # Use the *global* exit stack to manage contexts
            read_stream, write_stream = await mcp_exit_stack.enter_async_context(stdio_client(server_params))
            session = await mcp_exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            logger.info(f"Initialized session with MCP server '{server_id}'.")

            response = await session.list_tools()
            server_tools = response.tools
            logger.info(f"Server '{server_id}' provides {len(server_tools)} tools.")
            for tool in server_tools:
                discovered_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema,  # Assuming MCP schema is compatible
                            # 'required' field might be nested differently, adjust if needed based on MCP spec
                        },
                    }
                )
                tool_map[tool.name] = server_id
                logger.info(f" -> Discovered tool: {tool.name}")
            return session, discovered_tools, tool_map

        except Exception as e:
            logger.error(f"Failed for MCP server '{server_id}': {e}", exc_info=True)
            return None, [], {} # Return empty on failure

    # --- Create connection tasks ---
    connection_tasks = [
        _connect_and_discover_mcp(server_id, server_conf)
        for server_id, server_conf in mcp_server_configs.items()
    ]

    # --- Run tasks concurrently ---
    logger.info(f"Starting parallel connection to {len(connection_tasks)} MCP server(s)...")
    results = await asyncio.gather(*connection_tasks, return_exceptions=True)
    logger.info("Finished parallel MCP connection attempts.")

    # --- Process results ---
    for i, result in enumerate(results):
        server_id = list(mcp_server_configs.keys())[i] # Get corresponding server_id
        if isinstance(result, Exception):
            logger.error(f"Gather caught exception for server '{server_id}': {result}")
        elif result:
            session, discovered, tool_map = result
            if session:
                mcp_sessions[server_id] = session # Store successful session
                mcp_tools.extend(discovered)
                tool_name_to_server_id.update(tool_map)
            else:
                logger.warning(f"Connection/discovery seems to have failed silently for server '{server_id}' (result: {result}).")
        else:
             logger.warning(f"Received unexpected empty result for server '{server_id}'.")


    logger.info(f"Finished MCP setup. Active sessions: {len(mcp_sessions)}. Total discovered tools: {len(mcp_tools)}")


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
    raise ValueError(
        "Telegram Bot Token must be provided via --telegram-token or TELEGRAM_BOT_TOKEN env var"
    )
if not args.openrouter_api_key:
    raise ValueError(
        "OpenRouter API Key must be provided via --openrouter-api-key or OPENROUTER_API_KEY env var"
    )

# Set OpenRouter API key for LiteLLM
os.environ["OPENROUTER_API_KEY"] = args.openrouter_api_key


# --- Helper Functions & Context Managers ---
@contextlib.asynccontextmanager
async def typing_notifications(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str = ChatAction.TYPING
):
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
                await asyncio.sleep(5)  # Avoid busy-looping on persistent errors

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
    await update.message.reply_text(
        f"Hello! I'm your family assistant. Your chat ID is `{chat_id}`. How can I help?"
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles regular text messages, potentially with photos, and forwards them to the LLM."""
    # Use caption if photo exists, otherwise use text
    user_message_text = update.message.caption or update.message.text or ""
    chat_id = update.effective_chat.id
    photo_content_part = None # Initialize photo part

    # --- Access Control ---
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Ignoring message from unauthorized chat_id {chat_id}")
        return

    logger.info(f"Received message from chat_id {chat_id}: {user_message_text}") # Fix variable name

    messages: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    user_message_timestamp = (
        update.message.date or now
    )  # Use message timestamp if available

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
                logger.warning(
                    f"Replied-to message {replied_msg_id} not found in DB, using direct content."
                )
        # Optional: Could fetch *more* history around the replied message here
    else:
        # If not a reply, add recent history from DB
        history_messages = await get_recent_history(
            chat_id,
            limit=MAX_HISTORY_MESSAGES,
            max_age=timedelta(hours=HISTORY_MAX_AGE_HOURS),
        )
        messages.extend(history_messages)
        logger.debug(f"Added {len(history_messages)} recent messages from DB history.")

    # --- Prepare current user message text part with sender/forward context ---
    user = update.effective_user
    user_name = user.first_name if user else "Unknown User"
    forward_context = ""
    if update.message.forward_origin:
        origin = update.message.forward_origin
        original_sender_name = "Unknown Sender"
        if origin.sender_user:
            original_sender_name = origin.sender_user.first_name or "User"
        elif origin.sender_chat:
            original_sender_name = origin.sender_chat.title or "Chat/Channel"

        forward_context = f"(forwarded from {original_sender_name}) "
        logger.debug(f"Message was forwarded from {original_sender_name}")

    # Include text message/caption in the formatted content
    formatted_user_text_content = f"Message from {user_name}: {forward_context}{user_message_text}".strip()
    text_content_part = {"type": "text", "text": formatted_user_text_content}

    # --- Handle Photo Attachment ---
    if update.message.photo:
        logger.info("Message contains photo. Processing...")
        try:
            photo_size = update.message.photo[-1]  # Highest resolution
            photo_file = await photo_size.get_file()
            # Download as byte array
            async with io.BytesIO() as buf:
                await photo_file.download_to_memory(out=buf)
                buf.seek(0)
                byte_array = buf.read()

            base64_image = base64.b64encode(byte_array).decode("utf-8")
            # Assuming JPEG, adjust if more complex detection is needed
            # TODO: Could try to infer from file_path if available, or use python-magic
            mime_type = "image/jpeg"
            photo_content_part = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
            }
            logger.info("Successfully processed and base64 encoded the photo.")
        except Exception as img_err:
            logger.error(f"Failed to process photo: {img_err}", exc_info=True)
            # Optionally inform the user or just proceed without the image
            await update.message.reply_text("Sorry, I had trouble processing the image.")
            # Don't add the photo part if processing failed
            photo_content_part = None


    # --- Assemble final message content (text + optional image) ---
    final_message_content_parts = [text_content_part]
    if photo_content_part:
        final_message_content_parts.append(photo_content_part)

    # Create the user message dictionary for the LLM
    current_user_message_content = {
        "role": "user",
        "content": final_message_content_parts, # Content is now a list
    }
    messages.append(current_user_message_content)

    llm_response = None
    try:
        # --- Prepare System Prompt Context ---
        system_prompt_template = PROMPTS.get(
            "system_prompt", "You are a helpful assistant."
        )  # Default prompt

        # 1. Current Time
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")

        # 2. Calendar Context
        calendar_context_str = ""
        if CALENDAR_CONFIG:  # Check the renamed config dict
            try:
                logger.info("Fetching calendar events...")
                # Pass the config to the fetch function
                upcoming_events = await calendar_integration.fetch_upcoming_events(CALENDAR_CONFIG)
                today_events_str, future_events_str = (
                    calendar_integration.format_events_for_prompt(
                        upcoming_events, PROMPTS
                    )
                )
                calendar_header_template = PROMPTS.get(
                    "calendar_context_header",
                    "{today_tomorrow_events}\n{next_two_weeks_events}",
                )
                calendar_context_str = calendar_header_template.format(
                    today_tomorrow_events=today_events_str,
                    next_two_weeks_events=future_events_str,
                ).strip()
                logger.info("Prepared calendar context.")
            except Exception as cal_err:
                logger.error(
                    f"Failed to fetch or format calendar events: {cal_err}",
                    exc_info=True,
                )
                # Include the specific error in the context for the LLM
                calendar_context_str = f"Error retrieving calendar events: {str(cal_err)}"
        else:
            logger.debug("No calendars configured, skipping calendar context.")
            calendar_context_str = "Calendar integration not configured."  # Inform LLM

        # 3. Notes Context
        notes_context_str = ""
        all_notes = await get_all_notes()
        if all_notes:
            notes_list_str = ""
            note_item_format = PROMPTS.get("note_item_format", "- {title}: {content}")
            for note in all_notes:
                notes_list_str += (
                    note_item_format.format(
                        title=note["title"], content=note["content"]
                    )
                    + "\n"
                )

            notes_context_header_template = PROMPTS.get(
                "notes_context_header", "Relevant notes:\n{notes_list}"
            )
            notes_context_str = notes_context_header_template.format(
                notes_list=notes_list_str.strip()
            )
            logger.info("Prepared notes context.")
        else:
            notes_context_str = PROMPTS.get("no_notes", "No notes available.")

        # --- Assemble Final System Prompt ---
        final_system_prompt = system_prompt_template.format(
            current_time=current_time_str,
            calendar_context=calendar_context_str,
            notes_context=notes_context_str,
        ).strip()

        if final_system_prompt:  # Only insert if there's content
            messages.insert(0, {"role": "system", "content": final_system_prompt})
            logger.info("Prepended system prompt to LLM messages.")
        else:
            logger.warning("Generated empty system prompt.")

        # Send typing action using context manager
        # Combine local tools and MCP tools
        # Ensure processing.TOOLS_DEFINITION is accessible or imported
        from processing import TOOLS_DEFINITION as local_tools_definition

        all_tools = local_tools_definition + mcp_tools
        # logger.debug(f"Providing {len(all_tools)} total tools to LLM ({len(local_tools_definition)} local, {len(mcp_tools)} MCP).") # Removed debug log

        async with typing_notifications(context, chat_id):
            # Get response from LLM via processing module, passing all available tools and MCP state
            llm_response = await get_llm_response(
                messages,
                args.model,
                all_tools,
                mcp_sessions,  # Pass the MCP sessions dict
                tool_name_to_server_id,  # Pass the tool name mapping
            )

        if llm_response:
            # Reply to the original message to maintain context in the Telegram chat
            # The llm_response here is the final response after potential tool calls
            # Convert the LLM's markdown response to Telegram's MarkdownV2 format
            try:
                converted_markdown = telegramify_markdown.markdownify(llm_response)
                await update.message.reply_text(converted_markdown, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as md_err: # Catch potential errors during conversion
                logger.error(f"Failed to convert markdown: {md_err}. Sending plain text.", exc_info=True)
                # Fallback to sending plain text if conversion fails
                await update.message.reply_text(llm_response)
        else:
            await update.message.reply_text("Sorry, I couldn't process that.")
            logger.warning("Received empty response from LLM.")

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        # Let the error_handler deal with notifying the developer
        await update.message.reply_text(
            "Sorry, something went wrong while processing your request."
        )
        # Re-raise the exception so the error handler catches it
        raise
    finally:
        # --- Store messages in DB ---
        try:
            # Store user message
            # Store user message: Store only the text part in history for simplicity
            # (DB content column expects text, storing base64 is too large)
            history_user_content = formatted_user_text_content # Start with the text part
            if photo_content_part:
                history_user_content += " [Image Attached]" # Add indicator to history
            await add_message_to_history(
                chat_id=chat_id,
                message_id=update.message.message_id,
                timestamp=user_message_timestamp,
                role="user",
                content=history_user_content, # Store text + indicator
            )
            # Store bot response if successful
            if llm_response:
                # We don't have the bot's message_id easily here without sending the reply first
                # For simplicity, using a placeholder or negative ID, or could refactor to store after reply
                # Using user's message_id + 1 as a pseudo-ID for now, might collide but unlikely for context
                bot_message_pseudo_id = update.message.message_id + 1
                await add_message_to_history(
                    chat_id=chat_id,
                    message_id=bot_message_pseudo_id,  # Placeholder ID
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    content=llm_response,
                )
        except Exception as db_err:
            logger.error(
                f"Failed to store message history in DB: {db_err}", exc_info=True
            )
            # Optionally notify developer about DB error


async def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception,
    # but as a list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(
        None, context.error, context.error.__traceback__
    )
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
                    chat_id=DEVELOPER_CHAT_ID,
                    text=message[i : i + max_len],
                    parse_mode=ParseMode.HTML,
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
    # Ensure the event is set to signal other parts of the application
    if not shutdown_event.is_set():
        shutdown_event.set()

    # --- Graceful Task Cancellation ---
    # Get the current loop *before* it might be stopped
    loop = asyncio.get_running_loop()
    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task()]
    if tasks:
        logger.info(f"Cancelling {len(tasks)} outstanding tasks...")
        for task in tasks:
            task.cancel()
        # Wait for tasks to finish cancelling
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Outstanding tasks cancelled.")
    else:
        logger.info("No outstanding tasks to cancel.")

    # --- Stop Services (Order might matter) ---
    if application and application.updater:
        logger.info("Stopping Telegram polling...")
        await application.updater.stop()
        logger.info("Telegram polling stopped.")

    # Uvicorn server shutdown is handled in main_async when shutdown_event is set

    # Close MCP sessions via the exit stack
    logger.info("Closing MCP server connections...")
    await mcp_exit_stack.aclose()
    logger.info("MCP server connections closed.")

    # Stop the application itself
    if application:
        logger.info("Shutting down Telegram application...")
        await application.shutdown()
        logger.info("Telegram application shut down.")


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
        ApplicationBuilder().token(args.telegram_token)
        # .persistence(persistence) # Removed persistence
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    # Handle text OR photo messages (with optional caption)
    # filters.PHOTO will match messages containing photos, potentially with captions
    # filters.TEXT will match plain text messages
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, message_handler
        )
    )

    # Register error handler
    application.add_error_handler(error_handler)

    # Initialize database schema
    await init_db()
    # Load MCP config and connect to servers
    await load_mcp_config_and_connect()

    # Initialize application (loads persistence, etc.)
    await application.initialize()

    # Start polling and job queue (if any)
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling started.") # Updated log message

    # --- Uvicorn Server Setup ---
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    # Run Uvicorn server concurrently (polling is already running)
    # telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES)) # Removed duplicate start_polling
    web_server_task = asyncio.create_task(server.serve())

    logger.info("Web server running on http://0.0.0.0:8000") # Updated log message

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

    # Polling task cancellation is handled by application.updater.stop() and application.shutdown()
    # No need to manually cancel telegram_task anymore.

    logger.info("All services stopped. Final shutdown.")
    # Application shutdown is handled by the signal handler which calls shutdown_handler


def main() -> None:
    """Sets up the event loop and signal handlers."""
    loop = asyncio.get_event_loop()

    # Setup signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda s=sig.name: asyncio.create_task(shutdown_handler(s))
        )

    # SIGHUP for config reload (only on Unix-like systems)
    if hasattr(signal, "SIGHUP"):
        try:
            loop.add_signal_handler(
                signal.SIGHUP, reload_config_handler, signal.SIGHUP, None
            )
        except NotImplementedError:
            logger.warning("SIGHUP signal handler not supported on this platform.")

    try:
        logger.info("Starting application...")
        loop.run_until_complete(main_async())
    except (KeyboardInterrupt, SystemExit) as ex:
        logger.warning(f"Received {type(ex).__name__}, initiating shutdown.")
        # Ensure shutdown runs if loop was interrupted directly
        if not shutdown_event.is_set():
            # Run the async shutdown handler within the loop
            loop.run_until_complete(shutdown_handler(type(ex).__name__))
    finally:
        # Task cleanup is now handled within shutdown_handler
        logger.info("Closing event loop.")
        loop.close()
        logger.info("Application finished.")


if __name__ == "__main__":
    main()
