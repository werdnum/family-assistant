---
name: Browser Automation
description: Guide for using the /browse and /browse_visual commands to navigate websites, fill forms, and perform multi-step web workflows.
---

# Browser Automation

Two browser profiles share a single headless Chromium tab per conversation. They never appear in the
same LLM context at once — each profile has its own tool set tuned for a different task shape.

## `/browse` — default, DOM-based

Best for reading pages and filling forms. The model works from an accessibility snapshot of the page
rather than pixel screenshots, so interactions are cheaper and faster.

- **Activation**: prefix your request with `/browse`.
- **Tools**: `browser_open`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_select`,
  `browser_extract`, `browser_wait`, `browser_screenshot`, `browser_exec` (JS escape hatch).
- **How it works**: each interaction uses a semantic ref like `e12` returned by the previous
  snapshot, not coordinates. Snapshots can be filtered with a `query` substring to keep context
  small.
- **Escape hatch**: `browser_exec` runs JavaScript in the page via `page.evaluate`. Use it when the
  fixed tools don't fit — shadow DOM, iframes, reading same-origin JSON endpoints, or multi-step DOM
  mutation in one turn.

### When to use `/browse`

- Read a page and answer a question about it.
- Search a site (open → fill → submit → extract).
- Click through links or navigate forms where elements have accessible labels.
- Pull structured data out of a same-origin JSON endpoint via `browser_exec`.

## `/browse_visual` — fallback, coordinate-based

Uses Gemini's native computer-use capability to click at pixel coordinates based on screenshots.
Reserved for tasks that genuinely can't be done from the DOM.

- **Activation**: prefix your request with `/browse_visual`.
- **Tools**: `click`, `double_click`, `triple_click`, `middle_click`, `right_click`, `move`,
  `mouse_down`, `mouse_up`, `type`, `press_key`, `key_down`, `key_up`, `hotkey`, `scroll`,
  `drag_and_drop`, `navigate`, `go_back`, `go_forward`, `take_screenshot`, `wait`.
- **How it works**: every action returns a screenshot; the model visually locates elements and
  commands clicks by coordinates.
- **Safety**: prompt-injection detection over screenshots is always on, and actions the model flags
  with a safety decision (payments, sending messages, accepting terms, …) pause for the user to
  confirm before they execute.

### When to use `/browse_visual`

- Interact with `<canvas>` elements, image maps, or custom drawing surfaces.
- Drag-and-drop that targets non-DOM drop zones.
- Sites that render text as images and have no accessibility tree.
- Anything the DOM profile tries and fails on.

## Delegation

`/browse` can delegate to `/browse_visual` via `delegate_to_service` when it hits a visual-only
task. The delegated agent picks up the same live browser tab (same `conversation_id`, same
`BrowserSession`) so state is preserved.

## Limitations (both profiles)

- No access to the user's logged-in accounts or passwords.
- Cannot download files to the user's device.
- Cannot bypass CAPTCHAs or paywalls.
- May be blocked by anti-bot protection on some sites.

## Tips

1. Prefer `/browse` by default — it's cheaper and faster. Fall back to `/browse_visual` only when
   the DOM path fails.
2. Provide full URLs including `https://`.
3. Break complex tasks into steps.
4. Describe what you're looking for so the model can filter the snapshot.
