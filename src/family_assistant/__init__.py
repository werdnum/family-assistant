"""Family Assistant Package."""

import logging
import logging.config
import os

# Configure logging as early as possible
LOGGING_CONFIG = os.getenv("LOGGING_CONFIG", "logging.conf")
if os.path.exists(LOGGING_CONFIG):
    logging.config.fileConfig(LOGGING_CONFIG, disable_existing_loggers=False)

# You can optionally define __version__ here or import key components
