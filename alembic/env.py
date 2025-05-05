import asyncio
from logging.config import fileConfig
import os

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from family_assistant.storage import metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
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
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # Get database URL from environment variable, similar to base.py
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is not set.")

    # Prepare the configuration dictionary for the engine
    cfg = config.get_section(config.config_ini_section, {}) # Get existing alembic config section
    cfg["sqlalchemy.url"] = db_url # Explicitly set the URL from env var

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    # Detect if an event loop is already running
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # No running event loop
        loop = None

    if loop and loop.is_running():
        # If loop is running, schedule the task and wait (or run directly if possible)
        # This might still be tricky depending on context, but await is better than asyncio.run
        # A more robust solution might involve checking context vars or passing flags.
        # For now, let's assume awaiting directly works if a loop exists.
        # Note: This await might need adjustments if run_migrations_online itself
        # isn't called from an async function context when invoked via run_sync.
        # Let's try running it synchronously if a loop is present, as run_sync expects a sync function.
        # A better approach might be needed if this still fails.
        task = loop.create_task(run_async_migrations())
        # This is tricky - we are in a sync function called by run_sync.
        # We can't directly await task here. Let's try running until complete.
        # This is still risky. The core issue is invoking async logic from run_sync triggering this path.
        loop.run_until_complete(task) # Attempt to run the async task within the existing loop
    else:
        # No loop running (e.g., command line execution), safe to use asyncio.run
        asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
