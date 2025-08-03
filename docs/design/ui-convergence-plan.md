# UI Convergence Plan: Eliminate Dual System

## Goal

Convert all 27 Jinja2 pages to React components served by Vite, eliminating the dual UI system.

## Current State

- **3 React pages** (chat, tools, tool-test-bench) ✅
- **27 Jinja2 pages** to migrate
- **Dual system complexity** in JS/CSS resource management

## Migration Categories

### Easy Conversions (Existing APIs) - 5 pages

- `/tools` (Jinja2 version) → Has tools API
- `/errors/` + `/errors/{id}` → Has errors API
- `/event-listeners/*` (5 pages) → Has full CRUD API
- `/chat/conversations` → Has chat API

### Need New APIs - 12 pages

All backend API endpoints are now implemented:

- `/notes` + `/notes/add` + `/notes/edit` → **Notes CRUD API implemented**
- `/tasks` → **Tasks list and retry API implemented**
- `/history` → **Conversation history API implemented**
- `/vector-search` + document detail → **Search APIs implemented**
- `/events` + `/events/{id}` → **Events read API implemented**
- `/documents/` list → **Documents list API implemented**

### Simple/Static - 10 pages

- Redirects, docs, auth pages, settings → Minimal API needs

## Implementation Strategy

### Recommended Infrastructure Sequence

**Phase 1: Foundation (1 session)**

- React Router setup
- Shared layout components
- Test with 1 simple page

**Phase 2: Page Conversions (incremental)**

- Convert page → Update routing → Remove old router
- Optionally refactor underlying API during conversion
- Repeat for each page

**Phase 3: Cleanup**

- Remove remaining Jinja2 infrastructure

### Infrastructure Project Analysis

#### 1. React Router Foundation Setup

**What:** Add React Router to existing Vite app, create shared layout components **Advantages:**

- Consistency across all page conversions
- Shared navigation component
- Proper browser history and deep linking
- Layout reuse (header, nav, footer) **Timing:** **DO FIRST** - foundational infrastructure that
  every conversion depends on

#### 2. Tools-First API Refactoring

**What:** Extract business logic into shared service layer, both tools and APIs call same
implementation **Advantages:**

- Single source of truth for business logic
- Consistency between tool and API behavior
- Better testability
- Easier maintenance **Timing:** **DURING CONVERSIONS** - refactor each API as you convert its UI
  (not blocking)

#### 3. Routing Infrastructure Updates

**What:** Update `vite.config.js` and `vite_pages.py` per page conversion **Advantages:**

- Incremental migration with no downtime
- Old and new systems coexist safely
- Easy rollback for individual pages **Timing:** **PER PAGE** - done as part of each page conversion

### Updated Session Plan

### Session 1: Foundation Setup ⬅️ **START HERE**

**Goal:** React Router infrastructure foundation

- Add React Router to existing Vite app
- Create shared layout components matching current navigation
- Test routing with one simple conversion (e.g., `/context` page)
- Establish patterns for all future conversions

### Session 2-3: First Conversions

**Goal:** Validate approach with easy wins

- Convert `/notes` pages → React components (Notes API ready)
- Convert `/tasks` → React dashboard (Tasks API ready)
- Establish page conversion workflow patterns

### Session 4-5: Complex Forms

**Goal:** Handle CRUD operations

- Convert `/event-listeners/*` (5 pages) → React forms (CRUD API exists)
- Handle JSON schema validation patterns

### Session 6-7: Search & History

**Goal:** Complex UI patterns

- Convert `/vector-search` + document detail → React search interface (API ready)
- Convert `/history` → React component (API ready)
- Convert `/events` + `/events/{id}` → React components (API ready)

### Session 8-9: Remaining Pages

**Goal:** Complete all conversions

- Convert `/documents/` list → React component (API ready)
- Convert simple/static pages (docs, auth, settings)
- Merge `/chat/conversations` into existing chat

### Session 10: Cleanup

**Goal:** Single UI system achieved

- Remove all Jinja2 routers (`*_ui.py`)
- Clean up routing configurations
- Verify no functionality regression

## Technical Approach

### React Architecture

- Extend existing Vite setup with React Router
- Reuse current styling (Simple.css + custom CSS)
- Match existing navigation and layout patterns
- Direct conversion of functionality (no enhancements)

### API Strategy: Tools-First Approach

Instead of building separate REST APIs, leverage existing tools infrastructure:

**Primary Pattern:** Refactor tools and APIs to use shared underlying methods

- Extract business logic into shared service layer functions
- Tools and REST APIs both call the same underlying implementation
- UI components use appropriate interface (tools API vs REST) based on needs
- Example: `notes_service.get_all_notes()` used by both `get_all_notes` tool and `/api/notes`
  endpoint

**Benefits:**

- Single source of truth for business logic
- Tools become testable through UI
- No code duplication between tool and API implementations
- Consistent behavior across LLM and human interfaces

**Implementation:**

- Extract shared logic to appropriate layer (service modules, repository methods, or existing
  utilities)
- Refactor existing tools to use shared implementation
- Build REST APIs that use same shared implementation
- UI components choose most appropriate interface

### Routing Updates

As we convert pages, we need to update two routing configurations:

**frontend/vite.config.js:**

- Initially: Add specific route rewrites for each converted page (e.g., `/notes` → `/notes.html`)
- Later optimization: Replace individual rewrites with generic pattern matching
- Consider fallback rule that tries `${path}.html` for any unmatched route

**src/family_assistant/web/routers/vite_pages.py:**

- Add new `@router.get()` endpoints for each converted page
- Serve the corresponding HTML entry point with `serve_vite_page()`
- Example: `@router.get("/notes")` → `serve_vite_page("notes.html")`

**Process for each page:**

1. Create React component and HTML entry point in `frontend/`
2. Add route rewrite in `vite.config.js`
3. Add endpoint in `vite_pages.py`
4. Test page loads correctly
5. Remove corresponding Jinja2 router

### Migration Strategy

- One page at a time
- Keep Jinja2 pages until React replacement ready
- Test each session's work before proceeding
- Focus on eliminating dual system, not adding features

## Success Criteria

- [ ] All Jinja2 pages converted to React
- [ ] All `*_ui.py` routers removed
- [ ] Single UI system (Vite only)
- [ ] No functionality regression
- [ ] Existing styling/UX preserved

## Non-Goals (for this project)

- UI/UX improvements
- Performance optimizations
- New features
- Mobile enhancements
- Advanced state management

The goal is convergence, not enhancement. Features can be added after we have a single UI system.

## Migration Status

### Completed ✅

**React Pages:**

- `/chat` - React (existing)
- `/tools` (React version) - React (existing)
- `/tool-test-bench` - React (existing)
- `/errors/` + `/errors/{id}` - React dashboard (converted)

**Backend APIs Completed:**

- [x] `/notes` + `/notes/add` + `/notes/edit` → Notes CRUD API complete
- [x] `/tasks` → Tasks list and retry API complete
- [x] `/history` → Conversation history API complete
- [x] `/vector-search` + document detail → **Vector search APIs complete with comprehensive
  filtering, proper schema validation, and extensive testing (15+ test scenarios)**
- [x] `/events` + `/events/{id}` → Events read API complete
- [x] `/documents/` list → Documents list API complete

### In Progress 🔄

- None

### Ready for React Conversion 🚀

All APIs are now implemented! The following pages are ready for React conversion:

**Easy Conversions (APIs exist):**

- [ ] `/event-listeners/*` (5 pages) → React forms (full CRUD API exists)
- [ ] `/chat/conversations` → Merge into existing chat (chat API exists)
- [ ] `/notes` + `/notes/add` + `/notes/edit` → React components (Notes API ready)
- [ ] `/tasks` → React dashboard (Tasks API ready)
- [ ] `/history` → React component (History API ready)
- [ ] `/vector-search` + document detail → React search interface (Search APIs ready)
- [ ] `/events` + `/events/{id}` → React components (Events API ready)
- [ ] `/documents/` list → React component (Documents API ready)

**Can Be Deleted (Obsolete):**

- [x] `/tools` (Jinja2 version) → Delete `tools_ui.py`, already have React version (completed)

**Simple/Static:**

- [ ] `/` (redirect) → Update redirect target
- [ ] `/docs/*` → React or keep simple
- [ ] `/context` → React
- [ ] `/settings/tokens` → React
- [ ] Auth pages (3) → React
- [ ] Document upload → React

### Jinja2 Routers to Remove

- [ ] `notes.py`
- [ ] `tasks_ui.py`
- [ ] `history.py`
- [ ] `vector_search.py`
- [ ] `events_ui.py`
- [ ] `documents_ui.py`
- [ ] `tools_ui.py`
- [ ] `context_viewer.py`
- [ ] `ui_token_management.py`
- [ ] `documentation.py`
- [ ] `errors.py`
- [ ] `listeners_ui.py`
- [ ] `chat_ui.py`

