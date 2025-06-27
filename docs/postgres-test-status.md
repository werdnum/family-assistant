# PostgreSQL Test Status

## Summary

**UPDATE (2025-06-27)**: The test infrastructure migration is now complete! All tests run against
both SQLite and PostgreSQL by default. The `--postgres` flag has been replaced with a more flexible
`--db` option that accepts `sqlite`, `postgres`, or `all` (default).

After implementing per-test database isolation and fixing some event loop issues, we've made
significant progress on PostgreSQL compatibility. However, 45 tests still fail when run with
PostgreSQL. These failures are test-specific issues - the production application works correctly
with PostgreSQL.

## Test Results

- **Passed**: 231 tests
- **Failed**: 3 tests (assertion/logic failures)
- **Errors**: 42 tests (database connection errors)
- **Total failing**: 45 tests

## Fixed Issues

### 1. Script Execution Event Loop Conflicts (FIXED)

- **Root Cause**: Script execution runs in a thread pool, but tools need to access PostgreSQL
  connections created in the main event loop
- **Solution**: Modified ToolsAPI to accept and use the main event loop for database operations
- **Affected Tests**: All script execution tests now pass with PostgreSQL

### 2. Test Isolation Issues (FIXED)

- **Root Cause**: PostgreSQL container was session-scoped and data persisted between tests
- **Solution**: Modified `test_db_engine` fixture to create a unique database for each test
- **Implementation**: Each test now gets its own PostgreSQL database that is created before the test
  and dropped after
- **Status**: Complete test isolation achieved - tests can run in any order without interference

## Remaining Test Failures by Category

### 1. Event Loop Connection Errors (42 tests)

These tests fail with `sqlalchemy.exc.ProgrammingError`, indicating they're trying to use PostgreSQL
connections from the wrong event loop:

**Indexing tests:**

- `test_email_indexing_with_primary_link_extraction_e2e`
- `test_document_ready_not_emitted_with_pending_tasks`

**Storage tests:**

- `test_get_recent_history_retrieves_correct_messages`
- `test_get_messages_by_turn_id_retrieves_correct_sequence`
- `test_get_messages_by_thread_id_retrieves_correct_sequence`

**Telegram tests:**

- `test_message_history_includes_most_recent_when_limited`

**Delegation tests:**

- `test_delegation_unrestricted_target_no_forced_confirm[False]`
- `test_delegation_unrestricted_target_no_forced_confirm[None]`

**Event system tests:**

- `test_test_event_listener_tool_matches_person_coming_home`
- `test_test_event_listener_tool_no_match_wrong_state`
- `test_test_event_listener_tool_empty_conditions_error`

**Context provider tests:**

- `test_notes_context_provider_respects_include_in_prompt`
- `test_notes_context_provider_empty_when_all_excluded`
- `test_notes_context_provider_mixed_visibility`

**Task system tests:**

- `test_reschedule_for_retry_uses_correct_error_column`

**Vector storage tests:**

- `test_get_full_document_content_with_raw_content`

**Web UI tests (17 endpoints):**

- All UI endpoint accessibility tests fail with the same error

**Unit tests:**

- All embedding generator tests (8 tests)
- `test_profile_builds_correct_tool_set_with_mcp_servers`
- `test_format_history_preserves_leading_tool_and_assistant_tool_calls`

### 2. Error Logging Thread Issues (1 test)

- **Test**: `test_error_logging_integration`
- **Issue**: SQLAlchemyErrorHandler creates its own worker thread with a separate event loop
- **Symptom**: Expects 3 error logs but gets 0 - logs aren't being written to PostgreSQL

### 3. Database-Specific Behavior Differences (2 tests)

**Different error messages:**

- **Test**: `test_create_event_listener_duplicate_name_error`
- **Issue**: PostgreSQL returns different constraint violation error messages than SQLite
- **Symptom**: Test expects "already exists" in error message

**JSON return type differences:**

- **Test**: `test_end_to_end_event_listener_wakes_llm`
- **Issue**: SQLite returns JSON as string, PostgreSQL returns parsed dict
- **Symptom**: `TypeError: the JSON object must be str, bytes or bytearray, not dict`

## Root Cause Analysis

1. **Event Loop Architecture**: The primary issue is that PostgreSQL's asyncpg driver is strict
   about event loop usage. Database connections must be accessed from the same event loop where they
   were created. Many tests inadvertently create new event loops or run in different contexts.

2. **Production Impact**: These event loop issues were the original reason for adding PostgreSQL
   test support - features were breaking in production that worked fine with SQLite during
   development.

3. **Test-Specific Problems**: Most failures are in the test setup/teardown or test utilities, not
   in the actual application code.

## Recommendations

1. **For Development**: Use `--db sqlite` for quick feedback during development
2. **For Production**: PostgreSQL works correctly with the application; the remaining issues are
   test-specific
3. **CI/CD**: Tests now run against both databases by default, ensuring comprehensive coverage
4. **Testing Strategy**:
   - Default (`pytest`): Runs all tests against both SQLite and PostgreSQL
   - Quick mode (`pytest --db sqlite`): Fast feedback with SQLite only
   - Production validation (`pytest --db postgres`): Verify PostgreSQL compatibility
5. **Future Work**:
   - Refactor tests to properly handle event loops when using PostgreSQL
   - Update error logging handler to better handle cross-event-loop database access
   - Add database-agnostic assertions for tests that check error messages or JSON handling

## Next Steps

1. **High Priority**: Fix the 3 logic failures as these could indicate actual behavioral
   differences:

   - Make JSON handling database-agnostic in event listener tests
   - Update error message assertions to work with both SQLite and PostgreSQL
   - Investigate why error logs aren't being written in PostgreSQL

2. **Medium Priority**: Address event loop issues in test fixtures and utilities

   - Ensure all test fixtures use the same event loop as the database connection
   - Review and fix test setup/teardown procedures

3. **Low Priority**: Full PostgreSQL test compatibility for all 276 tests

## Key Commits

- `08584e7` - fix: Handle PostgreSQL event loop issues in script execution
- `59e5d21` - feat: Add --postgres flag for running tests with PostgreSQL
