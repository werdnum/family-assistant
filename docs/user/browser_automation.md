# Browser Automation Guide

This guide explains how to use the Family Assistant's browser automation features to interact with
websites, fill out forms, and perform complex web tasks.

## Overview

Browser automation lets the assistant drive a headless browser on your behalf. Two profiles share
the same browser tab but expose different tool sets — they never appear in the same LLM context at
once, so each stays cheap and focused.

- **`/browse`** (default) — reads the page's accessibility tree and interacts with elements by
  semantic reference. Best for reading content and filling forms.
- **`/browse_visual`** (fallback) — uses a Google Gemini Computer Use model to click at pixel
  coordinates from screenshots. Best for `<canvas>`, image maps, and drag-and-drop on non-DOM
  surfaces.

By default, start with `/browse`. It's cheaper and faster. Fall back to `/browse_visual` only when
the DOM-based path cannot see what it needs to interact with.

## When to Use Browser Automation

### Good Use Cases

- **Complex web forms** — multi-step forms, login flows, or interactive applications
- **JavaScript-heavy sites** — pages that load content dynamically
- **Multi-step workflows** — tasks requiring navigation through multiple pages
- **Interactive research** — clicking through results, expanding sections, reading dynamic content

### When NOT to Use Browser Automation

- **Saving pages for later** — use "Save this page for later: [URL]" instead
- **Basic web searches** — the assistant can search the web without browser automation

## The `/browse` Command (default, DOM-based)

Prefix your request with `/browse` to use the semantic DOM profile:

```
/browse Go to example.com and find the contact form
/browse Search for recent reviews of the XZ-100 camera
/browse Navigate to the settings page and check my account status
```

### How `/browse` works

- Each interaction uses an **accessibility snapshot** of the page — a structured tree of roles,
  names, and references (like `e12`).
- The assistant clicks, fills, and selects by **semantic ref**, not by pixel coordinates.
- Snapshots can be filtered with a query substring to keep the context small, so the assistant stays
  focused on what matters.

### Available actions in `/browse`

- **`browser_open`** — navigate to a URL and return the page snapshot in one step.
- **`browser_snapshot`** — re-snapshot the current page, optionally filtered by a query.
- **`browser_click`** — click an element by its ref.
- **`browser_fill`** — fill an input by ref, optionally pressing Enter to submit.
- **`browser_select`** — select a `<select>` option by label or value.
- **`browser_extract`** — convert the current page (or a subtree) to Markdown.
- **`browser_wait`** — wait for a load state or CSS selector to appear.
- **`browser_screenshot`** — take an explicit screenshot to attach to the conversation.
- **`browser_exec`** — run JavaScript in the page (escape hatch for shadow DOM, iframes, reading
  same-origin JSON endpoints, or multi-step DOM mutation in a single turn).

## The `/browse_visual` Command (fallback, coordinate-based)

When the DOM path can't see what it needs — canvas drawings, image maps, drag-and-drop against pixel
targets — use `/browse_visual`:

```
/browse_visual Go to the drawing app and sketch a circle in the middle
/browse_visual On the map tool, click the red dot over Seattle
```

### How `/browse_visual` works

- Every action returns a screenshot; the model visually locates elements and commands clicks by
  coordinates.
- Uses Gemini's native computer-use capability (on Gemini 3.5 Flash), so it's somewhat slower and
  more expensive per turn than `/browse`.
- Available actions: single/double/triple/middle/right click, move/hover, type text, individual key
  presses and hotkey combinations, scroll, drag-and-drop, navigate, back/forward, screenshot, wait.
- **Prompt-injection detection** is always on: the model scans page screenshots for hidden
  adversarial instructions (e.g., invisible "ignore your instructions" text) and pauses for your
  confirmation instead of following them.
- **Safety confirmations**: when the model is about to do something consequential — confirm a
  payment, send a message, accept terms, modify account data — it pauses and asks you to approve
  first. You'll see the action and the model's explanation; approve to continue or decline to stop
  that action. Declining doesn't kill the session; the assistant is told and can adapt or wrap up.

## Delegation between profiles

`/browse` can hand off to `/browse_visual` when it hits a visual-only task. The handoff keeps the
same live browser tab (same conversation, same cookies, same page), so state is preserved across
profiles.

In practice you usually don't need to think about this — start with `/browse` and the assistant will
delegate when needed. You can also invoke `/browse_visual` directly if you know the task is
visual-only from the start.

## Handing the browser to a human (optional)

Some steps should never be done by the assistant: entering payment details, signing in with your
credentials, typing a one-time passcode, accepting legal consent, or solving a CAPTCHA. When the
optional **browser-server** integration is enabled, the assistant can hand the *live* browser
session over to you: it calls `browser_request_handoff` and replies with a one-time link. You open
the link, take control of the very same browser (rendered in your own window), finish the sensitive
step, and mark it done — then control returns to the assistant.

While you are in control, the assistant has **no** ability to see or drive that browser: it cannot
take screenshots, read the page, or run actions. This keeps secrets you type (passwords, card
numbers, OTPs) away from the model.

This capability is off unless your operator has configured the browser-server integration. When it
is not configured, the assistant will tell you it can't hand off and will ask you to do the step
yourself instead.

## Examples by Use Case

### Online Shopping

```
/browse Go to bestbuy.com and find the price of a 65-inch Samsung TV
```

### Information Research

```
/browse Check the opening hours for the Metropolitan Museum of Art
```

### Form Completion

```
/browse Go to the DMV appointment scheduler and show me available dates next week
```

### Visual / Canvas Tasks

```
/browse_visual On the drawing tool, drag the blue square into the green target zone
```

## Limitations

Both profiles share these limits:

- **No access to your logged-in accounts** — the browser session is separate from your personal
  browser, so saved passwords and active sessions are not visible.
- **No downloads to your device** — files downloaded go to the assistant's environment.
- **Cannot bypass CAPTCHAs or paywalls.**
- **May be blocked by anti-bot protection** on some sites.

## Privacy and Security

- The assistant can see everything displayed on pages it visits. With `/browse`, it reads the
  accessibility tree. With `/browse_visual`, it captures screenshots after every action.
- Browser sessions are isolated per conversation. No cookies or session data persist between
  conversations, and your personal browser is untouched.
- Never share passwords in a browser automation request.
- Be cautious with pages that display sensitive information — the content reaches the AI model.

## Tips for Best Results

1. **Default to `/browse`.** It's cheaper and faster. Fall back to `/browse_visual` only when the
   DOM path fails.
2. **Provide full URLs** including `https://`.
3. **Break complex tasks into steps** — multi-page workflows are easier to debug one step at a time.
4. **Describe what you're looking for** — "the blue Submit button at the bottom of the form" helps
   the assistant filter the snapshot or screenshot down to the relevant region.
5. **Be patient with slow pages.** The assistant can wait for content to load.

## Troubleshooting

### Page Won't Load

- Try providing the full URL with `https://`.
- Some sites block automated browsers; try a different approach or site.

### Can't Find an Element

- Describe what you're looking for more specifically.
- Ask the assistant to scroll down or look in a different section.
- If `/browse` can't see the element (e.g., it's inside a canvas or rendered as an image), try
  `/browse_visual`.

### Action Didn't Work

- Dynamic pages may require waiting for content to load.
- Ask the assistant to try again, or to take a snapshot/screenshot first to check the page state.

### Session Timeout

- Long-running browser sessions may time out. Break complex tasks into smaller requests.
- Start a new `/browse` session if needed.

## Related Features

- **[Research and Web Browsing](research-and-browsing.md)** — when to reach for `/browse` at all
- **[User Guide](USER_GUIDE.md)** — index of every topic guide
- **[Scripting Guide](scripting.md)** — automate tasks with scripts
- **Document Ingestion** — save web content: "Save this page for later: [URL]"
