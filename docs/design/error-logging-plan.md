# Error Logging to SQLAlchemy Table - Implementation Plan

## Overview

Add persistent error logging to a database table in addition to stderr output. This provides searchable error history that survives container restarts.

## Implementation Plan

### 1. Database Schema

```python
class ErrorLog(Base):
    __tablename__ = 'error_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=func.now(), index=True)
    logger_name = Column(String(255), nullable=False, index=True)
    level = Column(String(50), nullable=False, index=True)
    message = Column(Text, nullable=False)

    # Error details
    exception_type = Column(String(255))
    exception_message = Column(Text)
    traceback = Column(Text)

    # Context
    module = Column(String(255), index=True)
    function_name = Column(String(255))

    # Additional metadata
    extra_data = Column(JSON)  # For any additional context

```

### 2. SQLAlchemy Logging Handler

```python
class SQLAlchemyErrorHandler(logging.Handler):
    """Handler that writes ERROR and above to database."""

    def __init__(self, session_factory, min_level=logging.ERROR):
        super().__init__()
        self.session_factory = session_factory
        self.min_level = min_level
        self.consecutive_failures = 0
        self.circuit_breaker_threshold = 5

    def emit(self, record: logging.LogRecord) -> None:
        """Write log record to database."""
        if record.levelno < self.min_level:
            return

        # Circuit breaker
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            return

        try:
            session = self.session_factory()
            try:
                error_log = self._create_error_log(record)
                session.add(error_log)
                session.commit()
                self.consecutive_failures = 0  # Reset on success
            finally:
                session.close()
        except Exception:
            self.consecutive_failures += 1
            self.handleError(record)  # Fallback to stderr

    def _create_error_log(self, record: logging.LogRecord) -> ErrorLog:
        """Create ErrorLog from LogRecord."""
        exc_info = record.exc_info
        exception_type = None
        exception_message = None
        tb_text = None

        if exc_info:
            exception_type = exc_info[0].__name__ if exc_info[0] else None
            exception_message = str(exc_info[1]) if exc_info[1] else None
            tb_text = ''.join(traceback.format_exception(*exc_info))

        return ErrorLog(
            timestamp=datetime.fromtimestamp(record.created),
            logger_name=record.name,
            level=record.levelname,
            message=record.getMessage(),
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=tb_text,
            module=record.module,
            function_name=record.funcName
        )

```

### 3. Integration

Add handler during application startup:

```python
# In web_server.py or __main__.py
def setup_error_logging(session_factory):
    """Add database error handler to root logger."""
    handler = SQLAlchemyErrorHandler(session_factory)
    handler.setLevel(logging.ERROR)
    logging.getLogger().addHandler(handler)

```

### 4. Web UI for Viewing Errors

Add error viewing page at `/errors`:

```python
# web/routers/errors.py
@router.get("/errors")
async def error_list(
    session: AsyncSession = Depends(get_session),
    page: int = Query(1, ge=1),
    level: Optional[str] = None,
    logger: Optional[str] = None,
    days: int = Query(7, ge=1, le=90)
):
    """List recent errors with filtering."""
    cutoff_date = datetime.now() - timedelta(days=days)

    query = select(ErrorLog).where(ErrorLog.timestamp >= cutoff_date)

    if level:
        query = query.where(ErrorLog.level == level)
    if logger:
        query = query.where(ErrorLog.logger_name.contains(logger))

    query = query.order_by(ErrorLog.timestamp.desc())

    # Pagination
    offset = (page - 1) *50
    query = query.offset(offset).limit(50)

    result = await session.execute(query)
    errors = result.scalars().all()

    return templates.TemplateResponse(
        "errors.html",
        {"request": request, "errors": errors, "page": page}
    )

@router.get("/errors/{error_id}")
async def error_detail(
    error_id: int,
    session: AsyncSession = Depends(get_session)
):
    """Show detailed error with full traceback."""
    error = await session.get(ErrorLog, error_id)
    if not error:
        raise HTTPException(404)

    return templates.TemplateResponse(
        "error_detail.html",
        {"request": request, "error": error}
    )

```

### 5. Cleanup Job

Add scheduled task to remove old logs:

```python
async def cleanup_old_error_logs():
    """Remove error logs older than 30 days."""
    cutoff_date = datetime.now() - timedelta(days=30)

    async with get_session() as session:
        await session.execute(
            delete(ErrorLog).where(ErrorLog.timestamp < cutoff_date)
        )
        await session.commit()

```

### 6. Configuration

```yaml
# config.yaml
logging:
  database_errors:
    enabled: true
    min_level: ERROR
    retention_days: 30

```

## Benefits

1. **Persistent History**: Errors survive restarts
2. **Searchable**: Filter by time, level, logger, etc.
3. **Debugging**: Full tracebacks available
4. **Monitoring**: Track error patterns over time

## Testing

Create one integration test that:

1. Triggers an error through the application
2. Verifies it appears in the error_logs table
3. Checks that the web UI displays it correctly

```python
async def test_error_logging_integration():
    # Trigger an error
    with pytest.raises(ValueError):
        raise ValueError("Test error")

    # Check database
    async with get_session() as session:
        result = await session.execute(
            select(ErrorLog).where(ErrorLog.message.contains("Test error"))
        )
        error_log = result.scalar_one()

        assert error_log.exception_type == "ValueError"
        assert "Test error" in error_log.exception_message
        assert error_log.traceback is not None

```
