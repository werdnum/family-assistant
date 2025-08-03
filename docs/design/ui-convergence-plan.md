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

## Session Plan

### Session 1: Foundation Setup

**Goal:** Basic React routing infrastructure

- Add React Router to existing Vite app
- Create layout components matching current navigation
- Test routing with one simple conversion

### Session 2-3: Easy Wins

**Goal:** Establish API integration patterns

- Convert `/tools` Jinja2 → React (reuse existing API)
- Convert `/errors` pages → React dashboard
- Validate approach works

### Session 4-6: Notes System

**Goal:** Establish CRUD pattern

- Build notes API endpoints (GET, POST, PUT, DELETE)
- Convert notes pages → React components
- This becomes template for other CRUD conversions

### Session 7-8: Tasks & Events

**Goal:** Expand CRUD pattern

- Build tasks API, convert `/tasks` → React
- Build events read API, convert `/events` → React

### Session 9-10: Event Listeners

**Goal:** Handle complex forms

- Convert event listener CRUD → React forms
- Handle JSON schema validation

### Session 11-12: Search & History

**Goal:** Complex UI patterns

- Build search API, convert `/vector-search` → React
- Build history API, convert `/history` → React

### Session 13: Cleanup

**Goal:** Complete convergence

- Convert remaining simple pages
- Remove Jinja2 routers (`*_ui.py`)
- Single UI system achieved

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

