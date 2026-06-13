import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

# Import the FastAPI app (needed for app.state)
from family_assistant.web.app_creator import app as fastapi_app

# Import the Assistant class
from .assistant import Assistant

# Import configuration loading from the new module
from .config_loader import load_config

# --- Logging Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Keep external libraries less verbose
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.INFO)
logging.getLogger("caldav").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=None,
    help="Telegram Bot Token (overrides environment variable)",
)
parser.add_argument(
    "--openrouter-api-key",
    default=None,
    help="OpenRouter API Key (overrides environment variable)",
)
parser.add_argument(
    "--model",
    default=None,
    help="LLM model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    "--embedding-model",
    default=None,
    help="Embedding model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    "--embedding-dimensions",
    type=int,
    default=None,
    help="Embedding model dimensionality (overrides config file and environment variable)",
)
parser.add_argument(
    "--document-storage-path",
    default=None,
    help="Path to store uploaded documents (overrides config file and environment variable)",
)
parser.add_argument(
    "--attachment-storage-path",
    default=None,
    help="Path to store email attachments (overrides config file and environment variable)",
)


def main() -> int:
    """Loads config, parses args, sets up event loop, and runs the application."""
    config = load_config()
    args = parser.parse_args()

    # Apply CLI Overrides to config using model_copy for immutable updates
    # ast-grep-ignore: no-dict-any - Used for dynamic kwargs passed to model_copy()
    cli_overrides: dict[str, Any] = {}
    if args.telegram_token is not None:
        cli_overrides["telegram_token"] = args.telegram_token
    if args.openrouter_api_key is not None:
        cli_overrides["openrouter_api_key"] = args.openrouter_api_key
    if args.model is not None:
        cli_overrides["model"] = args.model
    if args.embedding_model is not None:
        cli_overrides["embedding_model"] = args.embedding_model
    if args.embedding_dimensions is not None:
        cli_overrides["embedding_dimensions"] = args.embedding_dimensions
    if args.document_storage_path is not None:
        cli_overrides["document_storage_path"] = args.document_storage_path
    if args.attachment_storage_path is not None:
        cli_overrides["attachment_storage_path"] = args.attachment_storage_path

    if cli_overrides:
        config = config.model_copy(update=cli_overrides)
        logger.info(f"Applied CLI overrides: {list(cli_overrides.keys())}")

    fastapi_app.state.config = config
    logger.info("Stored final AppConfig in FastAPI app state.")

    # LLM client overrides would be passed here if needed for main execution,
    # but typically this is for testing. For main run, it's None.
    assistant_app = Assistant(config, llm_client_overrides=None)
    loop = asyncio.get_event_loop()

    # Setup Signal Handlers
    signal_map = {signal.SIGINT: "SIGINT", signal.SIGTERM: "SIGTERM"}
    for sig_num, sig_name in signal_map.items():
        loop.add_signal_handler(
            sig_num,
            lambda name=sig_name, app_instance=assistant_app: (
                app_instance.initiate_shutdown(name)
            ),
        )

    try:
        logger.info("Starting application via Assistant class...")
        loop.run_until_complete(assistant_app.setup_dependencies())
        loop.run_until_complete(assistant_app.start_services())

        # After start_services() completes, ensure full stop_services logic runs.
        if not assistant_app.is_shutdown_complete():
            logger.info(
                "Ensuring all services are stopped post-start_services completion..."
            )
            loop.run_until_complete(assistant_app.stop_services())

    except ValueError as config_err:
        logger.critical(f"Configuration error during startup: {config_err}")
        return 1
    except (KeyboardInterrupt, SystemExit) as ex:
        logger.warning(
            f"Received {type(ex).__name__} in main, initiating shutdown sequence."
        )
        if not assistant_app.is_shutdown_complete():
            if not assistant_app.shutdown_event.is_set():
                assistant_app.initiate_shutdown(type(ex).__name__)
            logger.info(f"Ensuring stop_services is called due to {type(ex).__name__}")
            loop.run_until_complete(assistant_app.stop_services())
    except Exception as e:
        logger.critical(f"Unhandled exception in main: {e}", exc_info=True)
        if not assistant_app.is_shutdown_complete():
            logger.error("Attempting emergency shutdown due to unhandled exception.")
            if not assistant_app.shutdown_event.is_set():
                assistant_app.initiate_shutdown(
                    f"UnhandledException: {type(e).__name__}"
                )
            loop.run_until_complete(assistant_app.stop_services())
        return 1
    finally:
        # Ensure event loop cleanup
        remaining_tasks = [t for t in asyncio.all_tasks(loop=loop) if not t.done()]
        if remaining_tasks:
            logger.info(
                f"Cancelling {len(remaining_tasks)} remaining tasks in main finally block..."
            )
            for task in remaining_tasks:
                task.cancel()
            loop.run_until_complete(
                asyncio.gather(*remaining_tasks, return_exceptions=True)
            )
            logger.info("Remaining tasks cancelled.")

        logger.info("Closing event loop.")
        if not loop.is_closed():
            loop.close()

        logger.info("Application finished.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
