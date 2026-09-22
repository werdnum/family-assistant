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
  `browser_wait`, `browser_screenshot`, `browser_extract`, `browser_exec` (local and remote).
- **How it works**: each interaction uses a semantic ref like `e12` returned by the previous
  snapshot, not coordinates. Snapshots can be filtered with a `query` substring to keep context
  small.
- **Escape hatch**: `browser_exec` runs JavaScript in the page via `page.evaluate`. Use it when the
  fixed tools don't fit — shadow DOM, iframes, reading same-origin JSON endpoints, or multi-step DOM
  mutation in one turn.

### When to use `/browse`

- Read a page and answer a question about it.
- Search a site (open → fill → submit → snapshot).
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

## Signing in with Keychute

Use `/browse_authenticated` or delegate to `credential_browser_profile` for tasks requiring stored
credentials. When ordinary browsing encounters a login wall, delegate with the URL, objective, and
secret name if known. The credential profile starts a separate protected remote browser: ordinary
cookies, page state, and element refs do not transfer. Its visual helper is
`credential_browser_visual_profile`, which shares the protected tab. Ordinary browser profiles
cannot autofill; both retain their usual tools.

The credential profiles request `browser_autofill(secret_name="the-name-the-user-supplied")`. No
preconfigured site or standing grant is needed. If the name is unknown, ask the user for the
Keychute secret name, never its value. The browser checks the actual HTTPS page origin, and Keychute
approves or denies release; a standing grant can authorize later requests automatically.

On `approval_pending`, share the approval link when supplied and stop. Once the user approves and
asks to continue, retry the same fill without navigating away or reloading. Submit the form after
`filled`, inspect the result, and continue the task. Username-first login can use `kind="username"`
and then `kind="password"` on the next page. If the site explicitly rejects the credential, call
`browser_report_login_outcome(outcome="bad_password")` and stop rather than retrying passwords.

Credentials go directly from Keychute to the browser; tool results do not contain their values.
Credential browsers protect controls from creation and deny `browser_exec` and `browser_extract`.
Use snapshots and screenshots there. Ordinary browsers retain these advanced tools but cannot
request Keychute autofill. For MFA or CAPTCHA, use human browser handoff.

## Limitations (both profiles)

- Autofill does not return password values. Account access requires an approved credential release,
  an optional configured saved-site preset, or human login handoff.
- Cannot download files to the user's device.
- Cannot bypass CAPTCHAs or paywalls.
- May be blocked by anti-bot protection on some sites.

## Tips

1. Prefer `/browse` by default — it's cheaper and faster. Fall back to `/browse_visual` only when
   the DOM path fails.
2. Provide full URLs including `https://`.
3. Break complex tasks into steps.
4. Describe what you're looking for so the model can filter the snapshot.
