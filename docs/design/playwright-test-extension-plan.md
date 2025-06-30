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

### 4. Event Listeners (`test_event_listeners.py`)

- **Create Listener**: Fill form → Submit → See in list
- **Edit Listener**: Modify conditions → Save → Verify
- **Delete Listener**: Remove → Confirm → Verify gone
- **Toggle Active State**: Disable/enable → Verify state
- **Form Validation**: Invalid input → See field errors

### 5. Settings Management (`test_settings_flow.py`)

- **API Token Creation**: Generate token → Copy → Verify display
- **Token Deletion**: Delete token → Confirm → Verify removal
- **Settings Navigation**: Navigate between settings sections
- **Form Persistence**: Changes persist on page reload

### 6. Search and Discovery (`test_search_flow.py`)

- **Vector Search**: Enter query → See results → Click result
- **Empty Search**: No results → See helpful message
- **Search Filters**: Apply filters → See filtered results
- **Result Interaction**: Click result → Navigate to source

### 7. Chat Interface (`test_chat_flow.py`)

- **Send Message**: Type → Send → See response
- **Message History**: Scroll through past messages
- **Tool Execution Display**: See tool calls in UI
- **Error Messages**: API error → User-friendly message
- **Loading States**: Message sending shows spinner

### 8. Task Queue Monitoring (`test_task_queue.py`)

- **View Tasks**: See task list with statuses
- **Task Details**: Click task → See execution details
- **Auto-refresh**: Page updates without reload
- **Filter by Status**: Show only failed/pending tasks

### 9. Error Handling (`test_error_handling.py`)

- **API Errors**: Simulate 500 → See error message
- **Network Timeout**: Slow response → Timeout message
- **Form Validation**: Invalid data → Field-level errors
- **Session Expiry**: Expired session → Redirect to login

### 10. Cross-cutting Concerns (`test_common_features.py`)

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

### Phase 1: Foundation (Week 1)

1. Set up Page Object structure
2. Enhance test fixtures
3. Migrate `test_ui_endpoints.py`
4. Add console error checking

### Phase 2: Core Flows (Week 2)

1. Navigation tests
2. Notes management
3. Document management
4. Basic error handling

### Phase 3: Advanced Features (Week 3)

1. Event listeners
2. Settings/API tokens
3. Search functionality
4. Chat interface

### Phase 4: Polish (Week 4)

1. Task queue monitoring
2. Cross-cutting concerns
3. Visual regression setup
4. CI/CD integration

## Success Metrics

- **Coverage**: All major UI flows have at least one test
- **Reliability**: \<5% flake rate in CI
- **Speed**: Full suite runs in \<5 minutes
- **Maintainability**: Changes require updating \<3 test files
- **Developer Experience**: Clear test output and debugging

## Progress Tracking

### Phase 1: Foundation

- [x] Set up Page Object structure (2025-06-30)
- [x] Enhance test fixtures (2025-06-30)
- [x] Migrate `test_ui_endpoints.py` (2025-06-30)
- [x] Add console error checking (2025-06-30)

### Phase 2: Core Flows

- [ ] Navigation tests
- [ ] Notes management
- [ ] Document management
- [ ] Basic error handling

### Phase 3: Advanced Features

- [ ] Event listeners
- [ ] Settings/API tokens
- [ ] Search functionality
- [ ] Chat interface

### Phase 4: Polish

- [ ] Task queue monitoring
- [ ] Cross-cutting concerns
- [ ] Visual regression setup
- [ ] CI/CD integration

### Completed Items

<!-- Move completed items here with completion date -->

- Page Object structure created with BasePage class (2025-06-30)
- Enhanced test fixtures: authenticated_page, TestDataFactory, ConsoleErrorCollector (2025-06-30)
- Migrated test_ui_endpoints.py to Playwright with enhanced tests (2025-06-30)
- Console error checking integrated into all tests via fixture (2025-06-30)

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
