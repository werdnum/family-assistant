"""Fixture: engines built with the raw SQLAlchemy constructor must be flagged."""

from sqlalchemy.ext import asyncio as sa_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def build_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


def build_configured_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, echo=False, isolation_level="AUTOCOMMIT")


def build_qualified_engine(url: str) -> AsyncEngine:
    return sa_asyncio.create_async_engine(url)
