# Using External PostgreSQL for Tests

As of Phase 1.1 of the development container implementation, the test suite supports using an
external PostgreSQL instance instead of testcontainers.

## Environment Variable

Set `TEST_DATABASE_URL` to bypass testcontainers:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://user:password@host:port/database
```

## Usage Examples

### Local PostgreSQL

```bash
TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test_db pytest --db=postgres
```

### Docker Compose PostgreSQL

```bash
TEST_DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test pytest --db=postgres
```

### CI/CD Pipeline

```yaml
env:
  TEST_DATABASE_URL: ${{ secrets.TEST_DATABASE_URL }}
```

## Benefits

1. **Faster Tests**: No container startup overhead
2. **CI/CD Friendly**: Use managed databases in pipelines
3. **Development Containers**: Share PostgreSQL sidecar between containers
4. **Resource Efficiency**: Single database instance for all tests

## Implementation Details

The `postgres_container` fixture in `tests/conftest.py` checks for `TEST_DATABASE_URL`:

- If set: Returns a mock container using the external URL
- If not set: Falls back to testcontainers behavior

This maintains backward compatibility while enabling new deployment scenarios.
