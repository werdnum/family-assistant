import logging

import uvicorn

from family_assistant.web.app_creator import app

logger = logging.getLogger(__name__)

# --- Uvicorn Runner (for standalone testing or direct execution) ---
if __name__ == "__main__":
    logger.info("Starting Uvicorn server from web_server.py...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
