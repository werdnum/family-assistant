# Visual Documentation System

## Overview

This document outlines the design for a systematic screenshot generation system that captures all
major application flows across different viewports and themes for visual documentation and
regression testing purposes.

## Goals

- Generate comprehensive visual documentation of all major user flows
- Support multiple viewports (mobile, tablet, desktop) and themes (light, dark)
- Zero impact on regular CI test runs
- Easy maintenance and updates
- Clear organization and review interface

## Architecture

### Core Concept

The system leverages existing Playwright tests and page objects while creating a separate test suite
specifically for visual documentation. This approach avoids duplicating test logic while maintaining
clear separation between functional testing and visual documentation.

### Test Structure

```
tests/functional/web/
├── test_visual_documentation.py    # New: Visual documentation tests
├── pages/                          # Existing: Reused page objects
│   ├── chat_page.py
│   ├── base_page.py
│   └── ...
└── conftest.py                     # Enhanced: Visual documentation fixtures
```

### Screenshot Organization

```
visual-docs/
├── mobile-light/
│   ├── chat/
│   │   ├── 01-initial-load.png
│   │   ├── 02-new-conversation.png
│   │   ├── 03-message-sent.png
│   │   └── 04-tool-confirmation.png
│   ├── notes/
│   │   ├── 01-list-view.png
│   │   ├── 02-create-form.png
│   │   └── 03-edit-flow.png
│   └── navigation/
│       ├── 01-hamburger-menu.png
│       └── 02-sidebar-open.png
├── desktop-light/
│   └── [same structure]
├── desktop-dark/
│   └── [same structure]
├── index.html                      # Auto-generated review dashboard
└── metadata.json                   # Screenshot metadata and organization
```

## Implementation Details

### Test Markers and Execution

```python
# Tests are marked for visual documentation
@pytest.mark.visual_documentation
@pytest.mark.skipif(
    not os.getenv("GENERATE_VISUAL_DOCS"),
    reason="Visual documentation only runs when GENERATE_VISUAL_DOCS=1"
)
```

**Commands:**

```bash
# Generate all visual documentation
GENERATE_VISUAL_DOCS=1 pytest -m visual_documentation tests/functional/web/

# Generate specific viewport only
GENERATE_VISUAL_DOCS=1 VIEWPORT=mobile pytest -m visual_documentation tests/functional/web/

# Regular CI (visual tests don't run)
pytest tests/functional/web/  # Automatically skips visual_documentation marked tests
```

### Viewport and Theme Strategy

To avoid test explosion (163 tests × 6 combinations = 978 tests), we use selective combinations:

1. **Mobile + Light** (375×667) - Primary mobile experience validation
2. **Desktop + Light** (1280×720) - Primary desktop experience validation
3. **Desktop + Dark** (1280×720) - Dark mode validation

This covers the most important user scenarios while keeping the test suite manageable.

### Screenshot Utilities

```python
class VisualDocumentationHelper:
    """Helper class for managing screenshot capture and organization."""
    
    def __init__(self, page, viewport, theme):
        self.page = page
        self.viewport = viewport
        self.theme = theme
        self.step_counter = 1
        
    async def capture_step(self, flow_name: str, step_name: str, description: str = None):
        """Capture a screenshot for a specific step in a flow."""
        filename = f"{self.step_counter:02d}-{step_name}.png"
        path = f"visual-docs/{self.viewport}-{self.theme}/{flow_name}/{filename}"
        
        await self.page.screenshot(path=path, full_page=True)
        
        # Record metadata for dashboard generation
        self.record_screenshot_metadata(flow_name, step_name, filename, description)
        self.step_counter += 1
```

## Priority Flows

### Priority 1 (Must Have)

- **Chat Flow**: New conversation, sending messages, tool confirmations, profile switching
- **Notes Flow**: List view, create form, edit operations, delete confirmation
- **Documents Flow**: List view, upload interface, document details
- **Navigation Flow**: Menu states, mobile hamburger, responsive behavior

### Priority 2 (Should Have)

- **Vector Search Flow**: Search interface, results display, filtering
- **Events Flow**: List view, detail view, filtering interface
- **History Flow**: Conversation history, filtering, pagination
- **Settings Flow**: Token management interface

### Priority 3 (Nice to Have)

- **Tasks Flow**: Queue operations, status changes
- **Errors Flow**: Error details, filtering
- **Tools Flow**: Tool execution interface

## CI/CD Integration

### New Workflow: Visual Documentation

```yaml
# .github/workflows/visual-documentation.yml
name: Generate Visual Documentation

on:
  workflow_dispatch:  # Manual trigger initially
  # Future triggers:
  # push:
  #   paths: ['frontend/src/**']
  # schedule:
  #   - cron: '0 0 * * 0'  # Weekly

jobs:
  generate-screenshots:
    runs-on: ubuntu-latest
    steps:
      - name: Generate Visual Documentation
        run: |
          GENERATE_VISUAL_DOCS=1 pytest \
            -m visual_documentation \
            --screenshot=on \
            --html=visual-docs/report.html \
            tests/functional/web/test_visual_documentation.py
      
      - name: Upload Visual Documentation
        uses: actions/upload-artifact@v4
        with:
          name: visual-documentation
          path: visual-docs/
          retention-days: 30
```

### Regular CI: No Impact

Regular CI continues to run all tests except those marked with `visual_documentation`, ensuring zero
performance impact on standard development workflows.

## Review Dashboard

The system generates an HTML dashboard for easy screenshot review:

```html
<!-- Example dashboard structure -->
<!DOCTYPE html>
<html>
<head>
    <title>Visual Documentation - Family Assistant</title>
    <style>/* Responsive grid layout */</style>
</head>
<body>
    <nav><!-- Flow navigation --></nav>
    <main>
        <section class="flow-section" data-flow="chat">
            <h2>Chat Flow</h2>
            <div class="viewport-comparison">
                <div class="viewport mobile">
                    <h3>Mobile (375×667)</h3>
                    <img src="mobile-light/chat/01-initial-load.png" />
                </div>
                <div class="viewport desktop">
                    <h3>Desktop (1280×720)</h3>
                    <img src="desktop-light/chat/01-initial-load.png" />
                </div>
            </div>
        </section>
    </main>
</body>
</html>
```

## Maintenance Strategy

### When to Regenerate Screenshots

1. **Manual Trigger**: Developers can trigger via GitHub Actions UI
2. **Significant Frontend Changes**: When PR affects >5 files in `frontend/src/`
3. **Component Updates**: Changes to `frontend/src/components/ui/`
4. **Before Releases**: Tag pushes or release branches
5. **Scheduled**: Weekly regeneration to catch drift

### Keeping Screenshots Fresh

- Screenshots include timestamp metadata
- Dashboard shows age of screenshots
- CI workflow can be scheduled to run weekly
- Alerts when screenshots are >30 days old

## Benefits

1. **Visual Regression Baseline**: Establish baseline for detecting unintended UI changes
2. **Design Review Tool**: Easy visual review of UI consistency across viewports
3. **Documentation**: Visual guide for new developers and designers
4. **QA Validation**: Quick way to verify responsive design implementation
5. **Zero CI Impact**: No performance degradation on regular development

## Success Criteria

- [ ] Screenshots generated for all Priority 1 flows
- [ ] HTML dashboard provides easy review interface
- [ ] CI workflow runs successfully with manual trigger
- [ ] No impact on regular test performance (confirmed via timing comparison)
- [ ] Clear documentation for developers to maintain the system

## Future Enhancements

1. **Visual Regression Detection**: Compare screenshots between versions
2. **Accessibility Screenshots**: Capture with screen reader overlays
3. **Animation States**: Capture key animation frames
4. **Mobile Touch Indicators**: Show touch targets and interaction areas
5. **Performance Metrics**: Overlay Core Web Vitals on screenshots

## Migration Plan

1. **Phase 1**: Implement core infrastructure and Priority 1 flows
2. **Phase 2**: Add Priority 2 flows and HTML dashboard
3. **Phase 3**: Add automated triggers and scheduled generation
4. **Phase 4**: Integrate visual regression detection

This design ensures we can systematically document all UI states while maintaining development
velocity and CI performance.
