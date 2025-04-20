import argparse
import asyncio
import contextlib
import html
import argparse
import argparse
import asyncio
import base64  # Add base64
import contextlib
import html
import io  # Add io
import json
import logging
import os
import signal
import sys
import traceback
import uuid  # Add uuid
import yaml
import mcp  # Import MCP
from mcp import ClientSession, StdioServerParameters  # MCP specifics
from mcp.client.stdio import stdio_client  # MCP stdio client
from contextlib import AsyncExitStack  # For managing multiple async contexts
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple  # Added Tuple

import pytz # Added for timezone handling
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
import telegramify_markdown  # Import the new library
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
import storage  # Import the whole module for task queue functions

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

# --- Events for coordination ---
shutdown_event = asyncio.Event()
new_task_event = asyncio.Event()  # Event to notify worker of immediate tasks


# --- Constants ---
MAX_HISTORY_MESSAGES = 5  # Number of recent messages to include (excluding current)
HISTORY_MAX_AGE_HOURS = 24  # Only include messages from the last X hours
TASK_POLLING_INTERVAL = 5  # Seconds to wait between polling for tasks

# --- Global Variables ---
application: Optional[Application] = None
ALLOWED_CHAT_IDS: list[int] = []
DEVELOPER_CHAT_ID: Optional[int] = None
PROMPTS: Dict[str, str] = {} # Global dict to hold loaded prompts
CALENDAR_CONFIG: Dict[str, Any] = {} # Stores CalDAV and iCal settings
TIMEZONE_STR: str = "UTC" # Default timezone
# shutdown_event moved higher up
from collections import defaultdict  # Add defaultdict

mcp_sessions: Dict[str, ClientSession] = (
    {}
)  # Stores active MCP client sessions {server_id: session}
# --- State for message batching ---
chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Store the Update object and optional photo bytes
message_buffers: Dict[int, List[Tuple[Update, Optional[bytes]]]] = defaultdict(list)
processing_tasks: Dict[int, asyncio.Task] = {}
mcp_tools: List[Dict[str, Any]] = []  # Stores discovered MCP tools in OpenAI format
tool_name_to_server_id: Dict[str, str] = {}  # Maps MCP tool names to their server_id
mcp_exit_stack = AsyncExitStack()  # Manages MCP server process lifecycles


# --- Task Queue Handler Registry ---
# Maps task_type strings to async handler functions
# Handler functions should accept the task payload as an argument
# Example: async def handle_my_task(payload: Any): ...
TASK_HANDLERS: Dict[str, callable] = {}


# Example Task Handler (can be moved elsewhere later)
async def handle_log_message(payload: Any):
    """Simple task handler that logs the received payload."""
    logger.info(f"[Task Worker] Handling log_message task. Payload: {payload}")
    # Simulate some work
    await asyncio.sleep(1)
    # In a real handler, you might interact with APIs, DB, etc.
    # If this function raises an exception, the task will be marked 'failed'.


# Register the example handler
TASK_HANDLERS["log_message"] = handle_log_message


async def handle_llm_callback(payload: Any):
    """Task handler for LLM scheduled callbacks."""
    global application  # Need access to the bot application instance
    if not application:
        logger.error(
            "Cannot handle LLM callback: Telegram application not initialized."
        )
        return

    chat_id = payload.get("chat_id")
    callback_context = payload.get("callback_context")

    if not chat_id or not callback_context:
        logger.error(f"Invalid payload for llm_callback task: {payload}")
        # Optionally mark task as failed here?
        return

    logger.info(f"Handling LLM callback for chat_id {chat_id}")
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    message_to_send = f"System Callback: The time is now {current_time_str}.\n\nYou previously scheduled a callback with the following context:\n\n---\n{callback_context}\n---"

    try:
        # Send the message *as the bot* into the chat.
        # Construct the trigger message content for the LLM
        # Using a clear prefix to indicate it's a callback
        trigger_text = f"System Callback Trigger:\n\nThe time is now {current_time_str}.\nYour scheduled context was:\n---\n{callback_context}\n---"
        trigger_content_parts = [{"type": "text", "text": trigger_text}]

        # Generate the LLM response using the refactored function
        # Use a placeholder name like "System" or "Assistant" for the user_name in the prompt
        llm_response = await _generate_llm_response_for_chat(
            chat_id=chat_id,
            trigger_content_parts=trigger_content_parts,
            user_name="System Trigger" # Or "Assistant"? Needs testing for optimal LLM behavior.
        )

        if llm_response:
            # Send the LLM's response back to the chat
            formatted_response = format_llm_response_for_telegram(llm_response)
            await application.bot.send_message(
                chat_id=chat_id,
                text=formatted_response,
                parse_mode=ParseMode.MARKDOWN_V2
                # Note: We don't have an original message ID to reply to here.
            )
            logger.info(f"Sent LLM response for callback to chat {chat_id}.")

            # Store the callback trigger and response in history
            try:
                # Pseudo-ID for the trigger message (timestamp based?)
                trigger_msg_id = int(datetime.now(timezone.utc).timestamp() * 1000) # Crude pseudo-ID
                await storage.add_message_to_history(
                    chat_id=chat_id,
                    message_id=trigger_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="system", # Or 'user'/'assistant' depending on how trigger_message was structured
                    content=trigger_text,
                )
                # Pseudo-ID for the bot response
                response_msg_id = trigger_msg_id + 1
                await storage.add_message_to_history(
                    chat_id=chat_id,
                    message_id=response_msg_id,
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    content=llm_response,
                )
            except Exception as db_err:
                logger.error(f"Failed to store callback history for chat {chat_id}: {db_err}", exc_info=True)

        else:
            logger.warning(f"LLM did not return a response for callback in chat {chat_id}.")
            # Optionally send a generic failure message to the chat
            await application.bot.send_message(
                chat_id=chat_id,
                text="Sorry, I couldn't process the scheduled callback."
            )
            # Raise an error to mark the task as failed if no response was generated
            raise RuntimeError("LLM failed to generate response for callback.")

    except Exception as e:
        logger.error(
            f"Failed during LLM callback processing for chat {chat_id}: {e}", exc_info=True
        )


# Register the callback handler
TASK_HANDLERS["llm_callback"] = handle_llm_callback

logger.info(f"Registered task handlers: {list(TASK_HANDLERS.keys())}")


# --- Helper Function ---
def format_llm_response_for_telegram(response_text: str) -> str:
    """Converts LLM Markdown to Telegram MarkdownV2, with fallback."""
    try:
        # Attempt conversion
        converted = telegramify_markdown.markdownify(response_text)
        # Basic check: ensure conversion didn't result in empty/whitespace only
        if converted and not converted.isspace():
            return converted
        else:
            logger.warning(f"Markdown conversion resulted in empty string for: {response_text[:100]}... Using original.")
            # Fallback to original text, escaped, if conversion is empty
            return escape_markdown(response_text, version=2)
    except Exception as md_err:
        logger.error(
            f"Failed to convert markdown: {md_err}. Falling back to escaped text. Original: {response_text[:100]}...",
            # exc_info=True, # Optional: Add full traceback if needed
        )
        # Fallback to escaping the original text
        return escape_markdown(response_text, version=2)


# --- Task Queue Worker ---
async def task_worker_loop(worker_id: str, wake_up_event: asyncio.Event):
    """Continuously polls for and processes tasks."""
    logger.info(f"Task worker {worker_id} started.")
    task_types_handled = list(TASK_HANDLERS.keys())

    while not shutdown_event.is_set():
        task = None
        try:
            # Dequeue a task of a type this worker handles
            task = await storage.dequeue_task(
                worker_id, task_types_handled
            )  # Use storage.dequeue_task

            if task:
                logger.info(
                    f"Worker {worker_id} processing task {task['task_id']} (type: {task['task_type']})"
                )
                handler = TASK_HANDLERS.get(task["task_type"])

                if handler:
                    try:
                        # Execute the handler with the payload
                        await handler(task["payload"])
                        # Mark task as done if handler completes successfully
                        await storage.update_task_status(
                            task["task_id"], "done"
                        )  # Use storage.update_task_status
                        logger.info(
                            f"Worker {worker_id} completed task {task['task_id']}"
                        )
                    except Exception as handler_exc:
                        logger.error(
                            f"Worker {worker_id} failed task {task['task_id']} due to handler error: {handler_exc}",
                            exc_info=True,
                        )
                        # Mark task as failed
                        await storage.update_task_status(  # Use storage.update_task_status
                            task["task_id"], "failed", error=str(handler_exc)
                        )
                else:
                    # This shouldn't happen if dequeue_task respects task_types
                    logger.error(
                        f"Worker {worker_id} dequeued task {task['task_id']} but no handler found for type {task['task_type']}. Marking failed."
                    )
                    await storage.update_task_status(  # Use storage.update_task_status
                        task["task_id"],
                        "failed",
                        error=f"No handler registered for type {task['task_type']}",
                    )

            else:
                # No task found, wait for the polling interval OR the wake-up event
                try:
                    logger.debug(
                        f"Worker {worker_id}: No tasks found, waiting for event or timeout ({TASK_POLLING_INTERVAL}s)..."
                    )
                    # Wait for the event to be set, with a timeout
                    await asyncio.wait_for(
                        wake_up_event.wait(), timeout=TASK_POLLING_INTERVAL
                    )
                    # If wait_for completes without timeout, the event was set
                    logger.debug(f"Worker {worker_id}: Woken up by event.")
                    wake_up_event.clear()  # Reset the event for the next notification
                except asyncio.TimeoutError:
                    # Event didn't fire, timeout reached, proceed to next polling cycle
                    logger.debug(
                        f"Worker {worker_id}: Wait timed out, continuing poll cycle."
                    )
                    pass  # Continue the loop normally after timeout

        except asyncio.CancelledError:
            logger.info(f"Task worker {worker_id} received cancellation signal.")
            # If a task was being processed, try to mark it as pending again?
            # Or rely on lock expiry/manual intervention for now.
            # For simplicity, we just exit.
            break  # Exit the loop cleanly on cancellation
        except Exception as e:
            logger.error(
                f"Task worker {worker_id} encountered an error: {e}", exc_info=True
            )
            # If an error occurs during dequeue or status update, wait before retrying
            await asyncio.sleep(TASK_POLLING_INTERVAL * 2)  # Longer sleep after error

    logger.info(f"Task worker {worker_id} stopped.")


# --- Configuration Loading ---
def load_config():
    """Loads configuration from environment variables and prompts.yaml."""
    global ALLOWED_CHAT_IDS, DEVELOPER_CHAT_ID, PROMPTS, CALENDAR_CONFIG, TIMEZONE_STR # Added TIMEZONE_STR
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
    CALENDAR_CONFIG = {}  # Initialize the combined config dict
    caldav_enabled = False
    ical_enabled = False

    # CalDAV settings
    caldav_user = os.getenv("CALDAV_USERNAME")
    caldav_pass = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str = os.getenv("CALDAV_CALENDAR_URLS")
    caldav_urls = (
        [url.strip() for url in caldav_urls_str.split(",")] if caldav_urls_str else []
    )

    if caldav_user and caldav_pass and caldav_urls:
        CALENDAR_CONFIG["caldav"] = {
            "username": caldav_user,
            "password": caldav_pass,
            "calendar_urls": caldav_urls,
        }
        caldav_enabled = True
        logger.info(
            f"Loaded CalDAV configuration for {len(caldav_urls)} specific calendar URL(s)."
        )
    else:
        logger.info(
            "CalDAV configuration incomplete or disabled (requires USERNAME, PASSWORD, CALENDAR_URLS)."
        )

    # iCal settings
    ical_urls_str = os.getenv("ICAL_URLS")
    ical_urls = (
        [url.strip() for url in ical_urls_str.split(",")] if ical_urls_str else []
    )

    if ical_urls:
        CALENDAR_CONFIG["ical"] = {
            "urls": ical_urls,
        }
        ical_enabled = True
        logger.info(f"Loaded iCal configuration for {len(ical_urls)} URL(s).")
    else:
        logger.info("iCal configuration incomplete or disabled (requires ICAL_URLS).")

    if not caldav_enabled and not ical_enabled:
        logger.warning(
            "No calendar sources (CalDAV or iCal) are configured. Calendar features will be disabled."
        )
        CALENDAR_CONFIG = {} # Ensure it's empty if nothing is enabled

    # --- Timezone Config ---
    loaded_tz = os.getenv("TIMEZONE", "UTC")
    try:
        # Validate the timezone string using pytz
        pytz.timezone(loaded_tz)
        TIMEZONE_STR = loaded_tz
        logger.info(f"Using timezone: {TIMEZONE_STR}")
    except pytz.exceptions.UnknownTimeZoneError:
        logger.error(f"Invalid TIMEZONE '{loaded_tz}' specified in .env. Defaulting to UTC.")
        TIMEZONE_STR = "UTC" # Keep the default if invalid


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

    async def _connect_and_discover_mcp(
        server_id: str, server_conf: Dict[str, Any]
    ) -> Tuple[Optional[ClientSession], List[Dict[str, Any]], Dict[str, str]]:
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

        logger.info(
            f"Attempting connection and discovery for MCP server '{server_id}'..."
        )
        try:
            server_params = StdioServerParameters(
                command=command, args=args, env=resolved_env
            )
            # Use the *global* exit stack to manage contexts
            read_stream, write_stream = await mcp_exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await mcp_exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
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
            return None, [], {}  # Return empty on failure

    # --- Create connection tasks ---
    connection_tasks = [
        _connect_and_discover_mcp(server_id, server_conf)
        for server_id, server_conf in mcp_server_configs.items()
    ]

    # --- Run tasks concurrently ---
    logger.info(
        f"Starting parallel connection to {len(connection_tasks)} MCP server(s)..."
    )
    results = await asyncio.gather(*connection_tasks, return_exceptions=True)
    logger.info("Finished parallel MCP connection attempts.")

    # --- Process results ---
    for i, result in enumerate(results):
        server_id = list(mcp_server_configs.keys())[i]  # Get corresponding server_id
        if isinstance(result, Exception):
            logger.error(f"Gather caught exception for server '{server_id}': {result}")
        elif result:
            session, discovered, tool_map = result
            if session:
                mcp_sessions[server_id] = session  # Store successful session
                mcp_tools.extend(discovered)
                tool_name_to_server_id.update(tool_map)
            else:
                logger.warning(
                    f"Connection/discovery seems to have failed silently for server '{server_id}' (result: {result})."
                )
        else:
            logger.warning(
                f"Received unexpected empty result for server '{server_id}'."
            )

    logger.info(
        f"Finished MCP setup. Active sessions: {len(mcp_sessions)}. Total discovered tools: {len(mcp_tools)}"
    )


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


# --- Core LLM Interaction Logic ---

async def _generate_llm_response_for_chat(
    chat_id: int,
    trigger_content_parts: List[Dict[str, Any]], # Content of the triggering message (user text/photo or callback)
    user_name: str, # Name to use in system prompt
    # TODO: Consider passing context explicitly instead of relying on globals/args
    # context: ContextTypes.DEFAULT_TYPE # Needed for typing notifications?
) -> str | None:
    """
    Prepares context, message history, calls the LLM, and returns the response.

    Args:
        chat_id: The target chat ID.
        trigger_content_parts: List of content parts (text, image_url) for the triggering message.
        user_name: The user name to format into the system prompt.

    Returns:
        The final LLM response string, or None on error.
    """
    logger.debug(f"Generating LLM response for chat {chat_id}, triggered by: {trigger_content_parts[0].get('type', 'unknown')}")

    # --- History and Context Preparation ---
    messages: List[Dict[str, Any]] = []
    history_messages = await storage.get_recent_history( # Use storage directly
        chat_id,
        limit=MAX_HISTORY_MESSAGES,
        max_age=timedelta(hours=HISTORY_MAX_AGE_HOURS),
    )
    messages.extend(history_messages)
    logger.debug(f"Added {len(history_messages)} recent messages from DB history.")

    # --- Prepare System Prompt Context ---
    system_prompt_template = PROMPTS.get("system_prompt", "You are a helpful assistant.")
    try:
        local_tz = pytz.timezone(TIMEZONE_STR)
        current_local_time = datetime.now(local_tz)
        current_time_str = current_local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as tz_err:
        logger.error(f"Error applying timezone {TIMEZONE_STR}: {tz_err}. Defaulting time format.")
        current_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    calendar_context_str = ""
    if CALENDAR_CONFIG:
        try:
            upcoming_events = await calendar_integration.fetch_upcoming_events(CALENDAR_CONFIG)
            today_events_str, future_events_str = calendar_integration.format_events_for_prompt(upcoming_events, PROMPTS)
            calendar_header_template = PROMPTS.get("calendar_context_header", "{today_tomorrow_events}\n{next_two_weeks_events}")
            calendar_context_str = calendar_header_template.format(today_tomorrow_events=today_events_str, next_two_weeks_events=future_events_str).strip()
        except Exception as cal_err:
            logger.error(f"Failed to fetch or format calendar events: {cal_err}", exc_info=True)
            calendar_context_str = f"Error retrieving calendar events: {str(cal_err)}"
    else:
        calendar_context_str = "Calendar integration not configured."

    notes_context_str = ""
    try:
        all_notes = await storage.get_all_notes() # Use storage directly
        if all_notes:
            notes_list_str = ""
            note_item_format = PROMPTS.get("note_item_format", "- {title}: {content}")
            for note in all_notes:
                notes_list_str += note_item_format.format(title=note["title"], content=note["content"]) + "\n"
            notes_context_header_template = PROMPTS.get("notes_context_header", "Relevant notes:\n{notes_list}")
            notes_context_str = notes_context_header_template.format(notes_list=notes_list_str.strip())
        else:
            notes_context_str = PROMPTS.get("no_notes", "No notes available.")
    except Exception as note_err:
        logger.error(f"Failed to get notes for context: {note_err}", exc_info=True)
        notes_context_str = "Error retrieving notes."


    final_system_prompt = system_prompt_template.format(
        user_name=user_name,
        current_time=current_time_str,
        calendar_context=calendar_context_str,
        notes_context=notes_context_str,
    ).strip()

    if final_system_prompt:
        messages.insert(0, {"role": "system", "content": final_system_prompt})
        logger.debug("Prepended system prompt to LLM messages.")
    else:
        logger.warning("Generated empty system prompt.")


    # --- Add the triggering message content ---
    # Note: For callbacks, the role might ideally be 'system' or a specific 'callback' role,
    # but using 'user' is often necessary for the LLM to properly attend to it as the primary input.
    # If LLM behavior is odd, consider experimenting with the role here.
    trigger_message = {
        "role": "user", # Treat trigger as user input for processing flow
        "content": trigger_content_parts,
    }
    messages.append(trigger_message)
    logger.debug(f"Appended trigger message to LLM history: {trigger_message}")

    # --- Combine local and MCP tools ---
    # Ensure processing.TOOLS_DEFINITION is accessible or imported
    from processing import TOOLS_DEFINITION as local_tools_definition
    all_tools = local_tools_definition + mcp_tools

    # --- Call LLM ---
    # Note: Typing notifications are omitted here for simplicity in this refactored function.
    # They could be added back if needed, perhaps by passing the `context` object.
    try:
        llm_response = await get_llm_response(
            messages,
            chat_id, # Pass the current chat_id
            args.model,
            all_tools,
            mcp_sessions,
            tool_name_to_server_id,
        )
        return llm_response # Can be None if get_llm_response fails
    except Exception as e:
        logger.error(f"Error during LLM interaction for chat {chat_id}: {e}", exc_info=True)
        return None


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


# --- Message Queue Processing ---
async def process_chat_queue(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the message buffer for a given chat."""
    # This function now contains the core logic previously in message_handler
    global processing_tasks, message_buffers, chat_locks  # Access globals

    logger.debug(f"Starting process_chat_queue for chat_id {chat_id}")
    async with chat_locks[chat_id]:
        # buffered_batch: List[Tuple[Update, Optional[bytes]]]
        buffered_batch = message_buffers[chat_id][:]  # Get a copy
        message_buffers[chat_id].clear()  # Clear the buffer
        logger.debug(
            f"Cleared buffer for chat {chat_id}, processing {len(buffered_batch)} items."
        )

    if not buffered_batch:
        logger.info(
            f"Processing queue for chat {chat_id} called with empty buffer. Exiting."
        )
        return  # Nothing to process

    logger.info(
        f"Processing batch of {len(buffered_batch)} message update(s) for chat {chat_id}."
    )

    # --- Extract context from the last message in the batch ---
    last_update, _ = buffered_batch[-1]  # Get the last Update object
    user = last_update.effective_user
    user_name = user.first_name if user else "Unknown User"
    reply_target_message_id = (
        last_update.message.message_id if last_update.message else None
    )
    logger.debug(
        f"Extracted user='{user_name}', reply_target_id={reply_target_message_id} from last update."
    )

    # --- Combine text and find first photo from the batch ---
    all_texts = []
    first_photo_bytes = None
    forward_context = ""  # Reset forward context for the batch

    for update_item, photo_bytes in buffered_batch:
        if update_item.message:
            text = update_item.message.caption or update_item.message.text or ""
            if text:
                all_texts.append(text)
            if photo_bytes and first_photo_bytes is None:
                first_photo_bytes = photo_bytes
                logger.debug(
                    f"Found first photo in batch from message {update_item.message.message_id}"
                )
            # Check for forward context (use context from the *last* message for simplicity)
            # A more complex approach could prepend context for each forwarded message.
            if update_item.message.forward_origin:
                origin = update_item.message.forward_origin
                original_sender_name = "Unknown Sender"
                if origin.sender_user:
                    original_sender_name = origin.sender_user.first_name or "User"
                elif origin.sender_chat:
                    original_sender_name = origin.sender_chat.title or "Chat/Channel"
                forward_context = f"(forwarded from {original_sender_name}) "
                logger.debug(
                    f"Detected forward context from {original_sender_name} in last message."
                )

    combined_text = "\n\n".join(all_texts).strip()
    logger.debug(f"Combined text: '{combined_text[:100]}...'")

    # --- Prepare current user message content part (combined) ---
    formatted_user_text_content = f"{forward_context}{combined_text}".strip()
    text_content_part = {"type": "text", "text": formatted_user_text_content}
    trigger_content_parts = [text_content_part] # Start with text

    # --- Handle Photo Attachment (from the first photo found) ---
    if first_photo_bytes:
        try:
            base64_image = base64.b64encode(first_photo_bytes).decode("utf-8")
            mime_type = "image/jpeg"  # Assuming JPEG
            trigger_content_parts.append({ # Append photo part
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
            })
            logger.info("Added first photo from batch to trigger content.")
        except Exception as img_err:
            logger.error(f"Error encoding photo from batch: {img_err}", exc_info=True)
                await context.bot.send_message(
                    chat_id, "Error processing image in batch."
                )
                # Continue processing with just the text part if the image failed.
                trigger_content_parts = [text_content_part] # Reset to just text


    llm_response = None
    logger.debug(f"Proceeding with trigger content and user '{user_name}'.")

    try:
        # Use the new helper function to get the LLM response
        async with typing_notifications(context, chat_id):
             llm_response = await _generate_llm_response_for_chat(
                 chat_id=chat_id,
                 trigger_content_parts=trigger_content_parts, # Pass the combined content
                 user_name=user_name, # Pass the actual user's name
                 # context=context # Pass context if typing_notifications are moved inside _generate_llm_response_for_chat
             )

        if llm_response:
            # Reply to the *last* message in the batch
            # The llm_response here is the final response after potential tool calls
            # Convert the LLM's markdown response to Telegram's MarkdownV2 format
            try:
                converted_markdown = telegramify_markdown.markdownify(llm_response)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=converted_markdown,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_to_message_id=reply_target_message_id,  # Use the stored ID
                )
            except Exception as md_err:  # Catch potential errors during conversion
                logger.error(
                    f"Failed to convert markdown: {md_err}. Sending plain text.",
                    exc_info=True,
                )
                # Fallback to sending plain text if conversion fails
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=llm_response,
                    reply_to_message_id=reply_target_message_id,  # Use the stored ID
                )
        else:
            # Handle case where LLM gave no response
            logger.warning("Received empty response from LLM.")
            if reply_target_message_id:  # Only reply if we have a message ID
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Sorry, I couldn't process that request.",
                    reply_to_message_id=reply_target_message_id,
                )

    except Exception as e:
        logger.error(
            f"Error processing message batch for chat {chat_id}: {e}", exc_info=True
        )
        # Let the error_handler deal with notifying the developer
        if reply_target_message_id:  # Check if we have a message to reply to
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Sorry, something went wrong while processing your request.",
                    reply_to_message_id=reply_target_message_id,
                )
            except Exception as reply_err:
                logger.error(
                    f"Failed to send error reply to chat {chat_id}: {reply_err}"
                )
                # Attempt to send without replying as a fallback
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="Sorry, something went wrong while processing your request (reply failed).",
                    )
                except Exception as fallback_err:
                    logger.error(
                        f"Failed to send fallback error message to chat {chat_id}: {fallback_err}"
                    )

        # Log and finish the task to prevent breaking the handler loop
        logger.debug(
            f"Finished processing batch for chat {chat_id}, proceeding to finally block."
        )
    finally:
        # --- Store messages in DB (Store combined user message, single bot response) ---
        # Removed redundant outer try block here
        try:
            # Store combined user message
            # Use the ID of the *last* message in the batch as the representative ID
            history_user_content = combined_text  # Start with combined text
            if first_photo_bytes:  # Check if a photo was processed in the batch
                history_user_content += " [Image(s) Attached]"  # Add indicator
            if reply_target_message_id:
                await add_message_to_history(
                    chat_id=chat_id,
                    message_id=reply_target_message_id,  # Use last known message ID
                    timestamp=datetime.now(timezone.utc),  # Use processing time
                    role="user",
                    content=history_user_content,
                )
                # Store bot response if successful
                if llm_response:
                    # Need bot's actual message ID if possible, otherwise use pseudo-ID
                    bot_message_pseudo_id = reply_target_message_id + 1
                    await add_message_to_history(
                        chat_id=chat_id,
                        message_id=bot_message_pseudo_id,  # Placeholder ID
                        timestamp=datetime.now(timezone.utc),
                        role="assistant",
                        content=llm_response,
                    )
            else:
                logger.warning(
                    f"Could not store batched user message for chat {chat_id} due to missing message ID."
                )

        except Exception as db_err:
            logger.error(
                f"Failed to store batched message history in DB for chat {chat_id}: {db_err}",
                exc_info=True,
            )

        # --- Task Cleanup ---
        async with chat_locks[chat_id]:  # Ensure lock is held for task removal
            if processing_tasks.get(chat_id) is asyncio.current_task():
                processing_tasks.pop(chat_id, None)
                logger.info(f"Processing task for chat {chat_id} finished and removed.")
            else:
                # This case might happen if a new task was rapidly scheduled, though unlikely with the lock
                logger.warning(
                    f"Current task for chat {chat_id} doesn't match entry in processing_tasks during cleanup."
                )


# --- Original Message Handler (Now Buffers) ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buffers incoming messages and triggers processing if not already running."""
    global message_buffers, processing_tasks, chat_locks  # Access globals
    chat_id = update.effective_chat.id
    user_message_text = update.message.caption or update.message.text or ""
    photo_bytes = None

    # --- Access Control ---
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Ignoring message from unauthorized chat_id {chat_id}")
        return

    # --- Process Photo (if any) into bytes immediately ---
    if update.message.photo:
        logger.info(
            f"Message {update.message.message_id} from chat {chat_id} contains photo."
        )
        try:
            photo_size = update.message.photo[-1]
            photo_file = await photo_size.get_file()
            with io.BytesIO() as buf:
                await photo_file.download_to_memory(out=buf)
                buf.seek(0)
                photo_bytes = buf.read()
            logger.debug(
                f"Photo from message {update.message.message_id} loaded into bytes."
            )
        except Exception as img_err:
            logger.error(
                f"Failed to process photo bytes for message {update.message.message_id}: {img_err}",
                exc_info=True,
            )
            await update.message.reply_text("Sorry, error processing attached image.")
            # Don't buffer this message if photo processing failed critically
            return

    # --- Add message Update object and photo bytes to buffer under lock ---
    async with chat_locks[chat_id]:
        message_buffers[chat_id].append((update, photo_bytes))
        # Removed storing message_id in context.user_data
        buffer_size = len(message_buffers[chat_id])
        logger.info(
            f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}"
        )

        # --- Check if processing task needs to be started ---
        if chat_id not in processing_tasks or processing_tasks[chat_id].done():
            logger.info(f"Starting new processing task for chat {chat_id}.")
            task = asyncio.create_task(process_chat_queue(chat_id, context))
            processing_tasks[chat_id] = task
            # Add callback to remove task from dict upon completion/error
            task.add_done_callback(lambda t, c=chat_id: processing_tasks.pop(c, None))
        else:
            logger.info(
                f"Processing task already running for chat {chat_id}. Message added to buffer."
            )
        # Lock is released automatically here


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
    logger.info("Bot polling started.")  # Updated log message

    # --- Uvicorn Server Setup ---
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    # Run Uvicorn server concurrently (polling is already running)
    # telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES)) # Removed duplicate start_polling
    web_server_task = asyncio.create_task(server.serve())
    logger.info("Web server running on http://0.0.0.0:8000")  # Updated log message

    # Start the task queue worker, passing the notification event
    worker_id = f"worker-{uuid.uuid4()}"
    task_worker = asyncio.create_task(task_worker_loop(worker_id, new_task_event))

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
    # Task worker cancellation is handled by the main shutdown_handler.
    # No need to manually cancel telegram_task or task_worker anymore.

    logger.info("All services stopped. Final shutdown.")
    # Application shutdown is handled by the signal handler which calls shutdown_handler


def main() -> None:
    """Sets up the event loop and signal handlers."""
    loop = asyncio.get_event_loop()

    # Setup signal handlers
    signal_map = {
        signal.SIGINT: "SIGINT",
        signal.SIGTERM: "SIGTERM",
    }
    for sig_num, sig_name in signal_map.items():
        # Use a default argument in the lambda that captures the current sig_name
        loop.add_signal_handler(
            sig_num, lambda name=sig_name: asyncio.create_task(shutdown_handler(name))
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
