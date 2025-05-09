import asyncio
from logging.config import fileConfig
import logging
import os

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from family_assistant.storage import metadata

# Set up logger
logger = logging.getLogger(__name__)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# Only configure logging if a config file is set AND the root logger has no handlers,
# indicating that the main application hasn't configured logging yet.
if config.config_file_name is not None and not logging.root.handlers:
    logger.info(
        f"Alembic env.py: Configuring logging from {config.config_file_name} as root logger has no handlers."
    )
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = os.getenv("DATABASE_URL")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Pass the script location from the main config object
    # This is necessary when env.py is run via run_sync, as the context
    # might not automatically inherit all settings.
    script_location = config.get_main_option("script_location")

    dialect_name = connection.dialect.name
    logger.info(
        f"do_run_migrations: Starting... Dialect: {dialect_name}, Connection: {connection!r}"
    )
    logger.info(
        f"do_run_migrations: Configuring context with script_location: {script_location}"
    )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=target_metadata.schema,
        include_schemas=True,
        script_location=script_location,
    )

    # Safely get revision argument for logging
    try:
        revision_argument = context.get_revision_argument()
        logger.info(
            f"do_run_migrations: Context configured. Destination Revision: {revision_argument}"
        )
    except KeyError:
        logger.info(
            "do_run_migrations: Context configured. (No destination revision applicable for this command)"
        )
        revision_argument = "(N/A for this command)"  # Placeholder for logs below

    try:
        # SQLite does not support transactional DDL, so run migrations outside a transaction block.
        if dialect_name == "sqlite":
            # Use the safely retrieved or placeholder revision_argument
            logger.info(
                f"Running SQLite migrations (non-transactional DDL) for target: {revision_argument}"
            )
            context.run_migrations()
        else:
            with context.begin_transaction():
                # Use the safely retrieved or placeholder revision_argument
                logger.info(
                    f"Running migrations within transaction for target: {revision_argument}"
                )
                context.run_migrations()
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.exception(f"Error running migrations: {e}")
        raise  # Re-raise the exception after logging


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # Get database URL from environment variable, similar to base.py
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set.")
        raise ValueError("DATABASE_URL environment variable is not set.")

    # Prepare the configuration dictionary for the engine
    cfg = config.get_section(
        config.config_ini_section, {}
    )  # Get existing alembic config section
    cfg["sqlalchemy.url"] = db_url  # Explicitly set the URL from env var

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    logger.info("Connecting to database for migrations...")
    try:
        async with connectable.connect() as connection:
            logger.info("Connection established, running migrations synchronously.")
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()
        logger.info("Database connection closed after migrations.")
    except Exception as e:
        logger.exception(f"Failed to connect or run migrations: {e}")
        raise  # Re-raise the exception after logging


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    # --- Context-aware migration logic ---
    # Check if Alembic is being run with an existing connection
    # This typically happens when invoked via engine.connect().run_sync()
    connectable = context.config.attributes.get("connection", None)

    if connectable is None:
        logger.info("Running migrations in 'online' mode using asyncio.")
        # Standard CLI execution: No external connection provided.
        # Use the async engine setup
        asyncio.run(run_async_migrations())
    else:
        logger.info("Running migrations in 'online' mode using provided connection.")
        # Invoked via run_sync: Connection provided.
        # Run migrations synchronously using the existing connection.
        do_run_migrations(connectable)
        logger.info("Migrations finished using provided connection.")


if context.is_offline_mode():
    run_migrations_offline()

else:
    run_migrations_online()
