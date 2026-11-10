# Browser Automation Guide

This guide explains how Family Assistant's browser automation profiles navigate websites, fill
forms, and perform multi-step web workflows.

## Overview

Three browser modes are available:

- **`/browse`** (default, DOM-based) — reads the page's accessibility tree and interacts with
  elements by semantic reference (`e12`). Fastest and cheapest; best for reading pages, searching,
  and filling forms anonymously.
- **`/browse_visual`** (fallback, coordinate-based) — uses Gemini Computer Use (Gemini 3.5 Flash) to
  click at pixel coordinates from screenshots. Best for `<canvas>`, image maps, and non-DOM
  drag-and-drop.
- **`/browse_authenticated`** (credential browsing) — opens an isolated, protected browser session
  that can autofill stored Keychute credentials on demand. See
  [authenticated-sites.md](authenticated-sites.md) for the full guide, and for operator-configured
  saved-login site presets.

Start with `/browse` by default. For simple page summaries or web search that do not require
interaction, you do not need `/browse` at all (see
[research-and-browsing.md](research-and-browsing.md)).

## The `/browse` Command (DOM-based)

```
/browse Go to example.com and find the contact form
/browse Check the opening hours for the Metropolitan Museum of Art
```

### How `/browse` works

- Each interaction works from an **accessibility snapshot** — a structured tree of roles, names, and
  element references (`e12`).
- Element refs stay bound to their element for the life of the session across subsequent snapshots
  and actions, so the assistant does not need to re-snapshot between steps.
- If the page has changed and a ref's element is gone, hidden, or replaced, the action fails safely
  and returns a fresh snapshot so the assistant can pick a new target.
- Snapshots and actions accept an optional `query` filter substring so only the relevant portion of
  the page is returned.

### Available actions in `/browse`

- **`browser_open`** — navigate to a URL and return the page snapshot in one step
- **`browser_snapshot`** — re-snapshot the current page, optionally filtered by `query`
- **`browser_click`** — click an element by ref
- **`browser_fill`** — fill an input by ref, optionally pressing Enter to submit
- **`browser_select`** — select a `<select>` option by label or value
- **`browser_wait`** — wait for a load state or CSS selector
- **`browser_screenshot`** — attach an explicit screenshot to the conversation
- **`browser_extract`** — read page content as Markdown
- **`browser_exec`** — run in-page JavaScript (`page.evaluate`) when fixed actions do not fit
  (shadow DOM, iframes, same-origin JSON endpoints)

## The `/browse_visual` Command (coordinate-based fallback)

Use `/browse_visual` when elements are not represented in the DOM or accessibility tree:

```
/browse_visual Go to the drawing app and sketch a circle in the middle
/browse_visual On the map tool, click the red dot over Seattle
```

- Every action returns a screenshot; the model locates targets visually and acts by pixel
  coordinates (`click`, `double_click`, `triple_click`, `middle_click`, `right_click`, `move`,
  `mouse_down`/`mouse_up`, `type`, `press_key`, `key_down`/`key_up`, `hotkey`, `scroll`,
  `drag_and_drop`, `navigate`, `go_back`, `go_forward`, `take_screenshot`, `wait`).
- **Prompt-injection detection** is always on over screenshots; suspected adversarial instructions
  pause for confirmation.
- **Safety confirmations:** consequential actions (confirming payments, sending messages, accepting
  terms, modifying account data) pause and ask you to approve before executing. Declining stops that
  action without killing the session.

## Credential Browsing (`/browse_authenticated`)

When a task requires signing in with a password stored in Keychute, use `/browse_authenticated` (or
let `/browse` delegate to it when it encounters a login wall):

- Runs in a **separate, protected remote browser context**: cookies, page progress, and element refs
  from anonymous `/browse` do not transfer.
- Requests `browser_autofill(secret_name=...)` via Keychute (deriving a plausible secret name from
  the target service if you did not name one, and sharing a `/ui/requests/<id>` approval link when
  approval is required).
- Disables `browser_exec` and `browser_extract` so page scripts or prompt injections cannot read
  back credentials; uses snapshots and visual companion delegation instead.
- See [authenticated-sites.md](authenticated-sites.md) for the full approval flow, bad-password
  handling, and pre-configured authenticated sites.

## Delegation between profiles

- **`/browse` ↔ `/browse_visual`:** Shares the same live browser tab (same conversation, cookies,
  and page state).
- **`/browse` → `/browse_authenticated`:** Starts a fresh protected browser session so credentials
  remain isolated (cookies and progress do not carry over). Within credential browsing, visual steps
  delegate to its own visual companion sharing that protected tab.

## Handing the browser to a human (optional)

Steps that should only be performed by a person — entering payment details, manual sign-in, typing a
one-time passcode (MFA), accepting legal terms, or solving a CAPTCHA — can use human handoff when
the optional **browser-server** integration is configured. The assistant calls
`browser_request_handoff` and sends a one-time link. You open the link, complete the step in the
live browser, and hand control back. While you hold the browser, the assistant cannot see,
screenshot, or drive it.

## Boundaries and Privacy

- **Isolation:** Anonymous browser sessions are isolated per conversation and per delegated task
  tab; nothing persists across conversations and your personal browser is never touched.
- **Visibility:** Everything rendered on visited pages reaches the model (via accessibility
  snapshots or screenshots). Never type raw passwords into a chat request; use
  [authenticated-sites.md](authenticated-sites.md) or human browser handoff.
- **Downloads and anti-bot gates:** Files downloaded land in the assistant's environment rather than
  your device, and CAPTCHAs or anti-bot blocks require human handoff.

## Related Guides

- **[authenticated-sites.md](authenticated-sites.md)** — `/browse_authenticated` with Keychute and
  pre-configured authenticated sites
- **[research-and-browsing.md](research-and-browsing.md)** — when to use web search or page fetch
  instead of `/browse`
- **[USER_GUIDE.md](USER_GUIDE.md)** — index of all topic guides
