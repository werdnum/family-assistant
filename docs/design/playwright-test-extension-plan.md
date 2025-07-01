# Playwright Test Extension Plan

## Overview

This document outlines a plan to extend Playwright-based testing for the Family Assistant web UI,
focusing on end-to-end user flows while avoiding duplication of existing functional tests.

## Testing Philosophy

### Core Principles

1. **UI Flow Coverage**: Test user journeys through the UI, not business logic
2. **Hermetic Environment**: Use fake/disabled dependencies where possible
3. **Fast Feedback**: Tests should run quickly for development iteration
4. **Maintainability**: Use Page Object Model for resilient tests
5. **Realistic Testing**: Test with full JS/CSS using Vite dev server

### What to Test vs What NOT to Test

**DO Test:**

- UI interactions and navigation flows
- Form submissions and validation feedback
- Data display and formatting
- Error message display
- Loading states and transitions
- Responsive behavior
- Console errors and warnings

**DON'T Test:**

- Business logic already covered by functional tests
- Complex tool execution (use mock responses)
- Database consistency (covered by unit tests)
- External service integrations
- Performance characteristics (separate concern)

## Infrastructure Improvements

### 1. Page Object Model Implementation

Create page objects for major UI sections to improve maintainability:

```python
# tests/functional/web/pages/base_page.py
class BasePage:
    def __init__(self, page: Page):
        self.page = page
        self.base_url = page.url
    
    async def wait_for_load(self):
        await self.page.wait_for_load_state("networkidle")

# tests/functional/web/pages/notes_page.py
class NotesPage(BasePage):
    async def add_note(self, title: str, content: str):
        await self.page.fill("#note-title", title)
        await self.page.fill("#note-content", content)
        await self.page.click("#submit-note")
```

### 2. Enhanced Test Fixtures

Add these fixtures to `conftest.py`:

```python
@pytest.fixture
async def authenticated_page(web_test_fixture):
    """Page with simulated authentication if needed"""
    
@pytest.fixture
async def test_data_factory():
    """Factory for creating test data consistently"""
    
@pytest.fixture
async def console_error_checker(page):
    """Fixture to collect and assert on console errors"""
```

### 3. Test Utilities

Create utilities for common operations:

- Wait helpers for dynamic content
- Screenshot capture on failure
- Test data builders
- API response mockers

## Test Flows to Implement

### 1. Core Navigation Tests (`test_navigation.py`)

- **Homepage Navigation**: Verify all nav links work
- **Breadcrumb Navigation**: Test breadcrumb links
- **Mobile Menu**: Test hamburger menu on mobile viewport
- **404 Handling**: Verify unknown routes show error page
- **Console Error Check**: No JS errors during navigation

### 2. Notes Management (`test_notes_flow.py`)

- **Create Note Flow**: Add note → View in list → Open detail
- **Edit Note Flow**: Open existing → Edit → Save → Verify changes
- **Delete Note**: Delete → Confirm → Verify removal
- **Search Notes**: Type search → See filtered results
- **Empty State**: Verify UI when no notes exist

### 3. Document Management (`test_documents_flow.py`)

- **Upload Document**: Select file → Upload → See in list
- **View Document**: Click document → View metadata
- **Multiple Upload**: Upload several files at once
- **File Type Validation**: Try invalid file → See error
- **Progress Indication**: Large file shows upload progress

### 4. History/Message History (`test_history_flow.py`) - HIGH PRIORITY

- **View History**: Navigate to history → See message list
- **Message Details**: Click message → View full conversation
- **Filtering**: Filter by date range, user, or content
- **Pagination**: Navigate through multiple pages of history
- **Search History**: Search for specific messages or conversations
- **Export History**: Export conversation history (if available)

### 5. Event Listeners (`test_event_listeners.py`)

- **Create Listener**: Fill form → Submit → See in list
- **Edit Listener**: Modify conditions → Save → Verify
- **Delete Listener**: Remove → Confirm → Verify gone
- **Toggle Active State**: Disable/enable → Verify state
- **Form Validation**: Invalid input → See field errors

### 6. Events UI (`test_events_flow.py`)

- **View Events**: Navigate to events → See event list
- **Event Details**: Click event → View event data
- **Event Filtering**: Filter by type, date, source
- **Real-time Updates**: New events appear without refresh
- **Event Actions**: Trigger actions from event (if available)

### 7. Settings Management (`test_settings_flow.py`)

- **API Token Creation**: Generate token → Copy → Verify display
- **Token Deletion**: Delete token → Confirm → Verify removal
- **Token List**: View all tokens with metadata
- **Token Permissions**: Set/view token permissions (if available)
- **Settings Navigation**: Navigate between settings sections
- **Form Persistence**: Changes persist on page reload

### 8. Search and Discovery (`test_search_flow.py`)

- **Vector Search**: Enter query → See results → Click result
- **Empty Search**: No results → See helpful message
- **Search Filters**: Apply filters → See filtered results
- **Result Interaction**: Click result → Navigate to source

### 9. Error Logs Viewer (`test_error_logs.py`)

- **View Error Logs**: Navigate to /errors → See error list
- **Filter by Level**: Filter errors, warnings, info
- **Filter by Logger**: Filter by logger name
- **Time Range Filter**: Filter by date/time range
- **Error Details**: Click error → View full traceback
- **Pagination**: Navigate through pages of errors
- **Empty State**: Verify UI when no errors exist

### 10. Tools Page (`test_tools_flow.py`)

- **View Tools List**: Navigate to /tools → See available tools
- **Tool Details**: Click tool → View description and parameters
- **Tool Execution**: Execute tool with parameters (if UI allows)
- **Tool Results**: View execution results or status

### 11. Task Queue Monitoring (`test_task_queue.py`)

- **View Tasks**: Navigate to /tasks → See task list with statuses
- **Task Details**: Click task → See execution details
- **Auto-refresh**: Page updates without reload
- **Filter by Status**: Show only failed/pending tasks

### 12. Error Handling (`test_error_handling.py`) - LOW PRIORITY

- **API Errors**: Simulate 500 → See error message
- **Network Timeout**: Slow response → Timeout message
- **Form Validation**: Invalid data → Field-level errors
- **Session Expiry**: Expired session → Redirect to login

### 13. Cross-cutting Concerns (`test_common_features.py`)

- **Responsive Design**: Test key flows on mobile viewport
- **Keyboard Navigation**: Tab through forms and links
- **Loading States**: All async operations show feedback
- **Time Display**: Verify timezone handling

## Migration of Existing Tests

### Phase 1: Migrate `test_ui_endpoints.py`

Convert endpoint accessibility tests to Playwright:

- Use `page.goto()` for each endpoint
- Check for console errors
- Verify key elements present
- Add visual regression baseline

### Phase 2: Enhance with Interaction Tests

For each endpoint, add basic interaction:

- Click primary action button
- Fill and submit one form
- Verify one state change

## Anticipated Challenges

### 1. Async Operations

- **Challenge**: Many UI operations trigger async backend calls
- **Solution**: Use Playwright's built-in wait strategies and custom waiters

### 2. Test Data Management

- **Challenge**: Tests need consistent test data
- **Solution**: Create data factories and use database fixtures

### 3. Flaky Tests

- **Challenge**: UI tests can be flaky due to timing
- **Solution**: Proper wait strategies, avoid arbitrary delays

### 4. Mock Complexity

- **Challenge**: Some flows require complex mock setups
- **Solution**: Create reusable mock scenarios in fixtures

### 5. CI/CD Integration

- **Challenge**: Playwright tests need headless browser in CI
- **Solution**: Use Playwright's built-in CI configurations

### 6. Test Execution Time

- **Challenge**: UI tests are slower than unit tests
- **Solution**: Parallel execution, smart test selection

## Implementation Order

### Phase 1: Foundation ✅ COMPLETED

1. Set up Page Object structure
2. Enhance test fixtures
3. Migrate `test_ui_endpoints.py`
4. Add console error checking

### Phase 2: Core Flows ✅ COMPLETED

1. Navigation tests
2. Notes management
3. Document management
4. ~~Basic error handling~~ (moved to Phase 4)

### Phase 3: Advanced Features (IN PROGRESS)

Priority implementation order:

1. History/Message History - HIGH PRIORITY
2. Error Logs Viewer
3. Event listeners + Events UI
4. Settings/API tokens (comprehensive)
5. Search functionality
6. Tools page

### Phase 4: Polish

1. Task queue monitoring (/tasks)
2. Basic error handling
3. Cross-cutting concerns
4. Visual regression setup
5. CI/CD integration

## Success Metrics

- **Coverage**: All major UI flows have at least one test
- **Reliability**: \<5% flake rate in CI
- **Speed**: Full suite runs in \<5 minutes
- **Maintainability**: Changes require updating \<3 test files
- **Developer Experience**: Clear test output and debugging

## Progress Tracking

### Phase 1: Foundation ✅ COMPLETED (2025-06-30)

- [x] Set up Page Object structure
- [x] Enhance test fixtures
- [x] Migrate `test_ui_endpoints.py`
- [x] Add console error checking

### Phase 2: Core Flows ✅ COMPLETED

- [x] Navigation tests (basic navigation link testing completed)
- [x] Notes management (tests/functional/web/test_notes_flow.py - 7 tests passing)
- [x] Document management (tests/functional/web/test_documents_flow.py - 9 tests passing, 2 skipped)
  - Note: Upload tests skipped due to internal HTTP call limitation in test environment
- [x] Basic error handling (deprioritized - moved to Phase 4)

### Phase 3: Advanced Features (IN PROGRESS)

Priority Order:

1. [x] History/Message History - HIGH PRIORITY ✅ COMPLETED (2025-07-01)
2. [ ] Error Logs Viewer (/errors endpoint)
3. [ ] Event listeners
4. [ ] Events UI (/events endpoint)
5. [ ] Settings/API tokens (comprehensive tests for create/delete/copy)
6. [ ] Search functionality
7. [ ] Tools page (/tools endpoint)

### Phase 4: Polish

- [ ] Task queue monitoring
- [ ] Cross-cutting concerns
- [ ] Visual regression setup
- [ ] CI/CD integration

### Completed Items

#### Phase 1 Completion Details (2025-06-30)

1. **Page Object Structure**

   - Created `BasePage` class with common UI interaction methods
   - Implemented wait helpers, element visibility checks, and navigation methods
   - Fixed navigation return value to properly return Response object

2. **Enhanced Test Fixtures**

   - Created `ConsoleErrorCollector` for automatic console error checking
   - Added `TestDataFactory` for consistent test data generation
   - Implemented `WebTestFixture` dataclass for organized test state
   - Simplified infrastructure by removing Vite dev server from tests
   - Added frontend asset building fixture for production-like testing

3. **Migrated test_ui_endpoints.py**

   - Converted all 21 UI endpoint tests to Playwright
   - Added element checking and console error validation
   - Enhanced with HTML dumping for failed tests
   - Fixed all test expectations to match actual page structure
   - All 42 tests (SQLite + PostgreSQL) now passing

4. **Console Error Checking**

   - Integrated automatic console error collection via fixture
   - Added smart filtering for expected errors (404s, CSS)
   - Console errors automatically reported on test failure

5. **Additional Infrastructure Improvements**

   - Fixed `template_utils.py` to respect DEV_MODE environment variable
   - Updated `poe dev` task to set DEV_MODE=true
   - Fixed `/settings/tokens` endpoint to work when auth is disabled
   - Resolved Vite/API server startup sequencing issues
   - Added responsive design and form interaction tests

#### Phase 3 Progress (2025-07-01)

1. **History/Message History Tests COMPLETED**

   - Created `HistoryPage` page object with comprehensive interaction methods
   - Implemented test suite covering:
     - Page loading and navigation
     - UI element verification (filters, conversation groups, messages)
     - Empty state handling with tolerance for pre-existing data
     - Tool call display verification
     - Filter functionality (interface type, conversation, date range)
     - Pagination controls
     - Trace expansion functionality
   - Tests simplified to verify UI functionality without depending on specific test data
   - Note: Database isolation between test framework and dev server requires UI-only testing
     approach

## Next Steps

1. Review and approve this plan
2. Create Page Object base structure
3. Begin Phase 1 implementation
4. Set up CI/CD pipeline for Playwright tests
5. Document test writing patterns for team

## Appendix: Example Test Pattern

```python
async def test_create_note_full_flow(web_test_fixture):
    """Test complete note creation flow from UI"""
    page = web_test_fixture.page
    
    # Navigate to notes
    await page.goto("/notes")
    await page.wait_for_load_state("networkidle")
    
    # Click add note button
    await page.click("text=Add Note")
    
    # Fill form
    await page.fill("#note-title", "Test Note")
    await page.fill("#note-content", "Test content")
    
    # Submit
    await page.click("button[type=submit]")
    
    # Verify success
    await page.wait_for_selector("text=Note created successfully")
    
    # Verify note appears in list
    await page.goto("/notes")
    await expect(page.locator("text=Test Note")).to_be_visible()
```
