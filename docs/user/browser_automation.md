# Browser Automation Guide

This guide explains how to use the Family Assistant's browser automation features to interact with
websites, fill out forms, and perform complex web tasks.

## Overview

Browser automation lets the assistant drive a headless browser on your behalf:

- **`/browse`** (default) — reads the page's accessibility tree and interacts with elements by
  semantic reference. Best for reading content and filling forms anonymously.
- **`/browse_visual`** (fallback) — uses a Google Gemini Computer Use model to click at pixel
  coordinates from screenshots. Best for `<canvas>`, image maps, and drag-and-drop on non-DOM
  surfaces.
- **`/browse_authenticated`** (credential browsing) — opens an isolated, protected browser session
  that can request and autofill credentials stored in Keychute on demand.

For stored credentials, use `/browse_authenticated` or let the assistant delegate to it after
encountering a login wall. It opens a separate protected session and can request a named Keychute
secret without preconfigured sites. Ordinary browsing retains JavaScript and raw extraction; the
credential browser uses snapshots and visual actions. See
[signing into websites](authenticated-sites.md).

By default, start with `/browse`. It's cheaper and faster. Fall back to `/browse_visual` only when
the DOM-based path cannot see what it needs to interact with.

## When to Use Browser Automation

### Good Use Cases

- **Complex web forms** — multi-step forms, registration flows, or interactive applications
- **Account logins and authenticated portals** — tasks requiring signing in with stored Keychute
  credentials (via `/browse_authenticated`)
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
- A ref points at one element for as long as the session lasts. It keeps working across further
  snapshots and actions, so the assistant does not have to re-read the page between steps.
- If the page has moved on — the element is gone, hidden, or has become something else — the action
  fails instead of clicking the wrong thing, and the failure comes back with a fresh snapshot the
  assistant can pick a new target from.
- Snapshots can be filtered with a query substring to keep the context small, and that filter works
  on actions too, so a click or a fill can come back with just the part of the page that matters.

### Available actions in `/browse`

- **`browser_open`** — navigate to a URL and return the page snapshot in one step.
- **`browser_snapshot`** — re-snapshot the current page, optionally filtered by a query.
- **`browser_click`** — click an element by its ref.
- **`browser_fill`** — fill an input by ref, optionally pressing Enter to submit.
- **`browser_select`** — select a `<select>` option by label or value.
- **`browser_wait`** — wait for a load state or CSS selector to appear.
- **`browser_screenshot`** — take an explicit screenshot to attach to the conversation.
- **`browser_extract`** — read page content as Markdown.
- **`browser_exec`** — run in-page JavaScript when the fixed actions do not fit.

For stored-password login tasks, use `/browse_authenticated`, or let the assistant switch when it
encounters a login wall. That browser can request named Keychute credentials without site
registration. It uses a separate session: cookies and page progress do not transfer from `/browse`.
JavaScript and raw extraction are unavailable in the credential browser; it uses snapshots and
visual actions instead. See [signing into websites](authenticated-sites.md).

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

## The `/browse_authenticated` Command (credential browsing)

Prefix your request with `/browse_authenticated` when a task requires signing in with credentials
stored in Keychute:

```
/browse_authenticated Open https://octoprint.local and check the current 3D print job
/browse_authenticated Go to https://www.amazon.com and check my recent orders using secret 'amazon-password'
```

You can also start with ordinary `/browse`; if the assistant encounters a login wall, it can
automatically switch to credential browsing to handle the login.

### How `/browse_authenticated` uses Keychute

The credential browser integrates with Keychute to autofill stored credentials on demand:

- **No site configuration required**: You do not need preconfigured sites or standing grants ahead
  of time. The assistant can navigate to any URL and request autofill on demand.
- **Secret name derivation**: If you specify the secret name in your request (e.g.
  `secret 'amazon-password'`), the assistant requests that name. If the secret name is not known
  beforehand, the assistant does not stop to prompt you; it derives a plausible, sensible secret
  name based on the target site or service (e.g. `octoprint`, `3dprinter`, or the domain/app name).
  You can route the request to the correct secret directly in the Keychute UI during approval.
- **Direct credential delivery**: Keychute delivers credentials directly to the browser backend.
  Passwords and secret values are not returned in tool responses, and accessibility snapshots mask
  password controls. Note that read-back protection against an approved site is best effort: once an
  approved site receives a password, a hostile or compromised page could potentially echo it into
  visible page content.
- **Password-only secrets and username entry**: If a released secret contains only a password
  without a username, the assistant can enter the known username manually and autofill only the
  password. For multi-page or username-first login flows, the assistant fills the username on the
  first page and the password on the next.

### How approval works

- **Origin-scoped verification**: Keychute evaluates each credential request against the actual
  HTTPS origin of the page currently loaded in the browser.
- **Interactive approval link**: If Keychute requires user approval, the assistant pauses and shares
  the Keychute web UI approval link, which lives under `/ui/requests/<id>`.
- **Review and resume**: Open the link, review the target domain and requested secret, route the
  request to the correct stored secret if needed, and approve release. Then return to your chat and
  tell the assistant to continue. The assistant retries the fill without reloading the page or
  navigating away.
- **Standing grants**: To streamline future tasks on trusted sites, you can configure a standing
  grant in Keychute that automatically approves releases for specific website origins.
- **Bad password handling**: If the site rejects the credentials, the assistant records the
  rejection and immediately stops, discarding the failed browser session to prevent account
  lockouts. Correct the secret in Keychute, then ask the assistant to try again in the same
  conversation.
- **MFA and CAPTCHAs**: For multi-factor authentication codes or CAPTCHA challenges, the assistant
  can generate a browser handoff link so you can complete the step manually in your browser before
  handing control back.

### How it differs from anonymous `/browse`

- **Isolated browser session**: Ordinary `/browse` runs as an anonymous visitor.
  `/browse_authenticated` spins up a separate, protected remote browser context. Cookies, page
  progress, and DOM element refs from an ordinary `/browse` session *do not transfer* into the
  credential session.
- **Strict tool isolation**:
  - Anonymous `/browse` provides access to in-page JavaScript execution and raw page extraction, but
    cannot access Keychute autofill.
  - Credential browsing can request Keychute autofill, but deliberately disables in-page JavaScript
    execution and raw page extraction. This prevents prompt injection or malicious third-party
    scripts on visited pages from reading back credentials or session tokens.
- **Visual delegation companion**: Just as `/browse` delegates to `/browse_visual`, credential
  browsing can delegate visual steps to a visual companion when needed, sharing the same protected
  browser tab and credentials.
- **Ad-hoc URLs vs. Preconfigured Presets**: Unlike preconfigured authenticated sites (which are
  locked to operator-defined domains and saved cookie jars), `/browse_authenticated` allows general
  browsing across arbitrary URLs, relying on Keychute's dynamic release approvals to safeguard
  credentials.

### When to use `/browse_authenticated`

- **Use `/browse`** for public web research, reading articles, documentation, or submitting
  unauthenticated forms.
- **Use `/browse_authenticated`** whenever you need the assistant to log into an account, personal
  dashboard, home-lab appliance (e.g. OctoPrint, Home Assistant), or web service using a password
  stored in Keychute.
- **Use preconfigured authenticated sites** (see [authenticated-sites.md](authenticated-sites.md))
  for scheduled or recurring household tasks where an operator has pre-configured account access,
  saved cookie jars, and origin confinement (such as HelloFresh meal deliveries).

## Delegation between profiles

`/browse` can hand off to `/browse_visual` when it hits a visual-only task. The handoff keeps the
same live browser tab (same conversation, same cookies, same page), so state is preserved across
profiles.

If ordinary browsing encounters a login wall requiring stored credentials, the assistant can switch
to credential browsing automatically. Because this starts a separate protected session to keep
credentials safe, cookies and page progress do not transfer. Within credential browsing, the
assistant can also delegate visual tasks to its visual companion, preserving the protected session
and tab.

In practice you usually don't need to think about this — start with `/browse` and the assistant will
delegate when needed. You can also invoke `/browse_authenticated` or `/browse_visual` directly if
you know the task needs them from the start.

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

### Credential Browsing / Authenticated Tasks

```
/browse_authenticated Go to octoprint.local and report whether the bed temperature is at target
/browse_authenticated Open https://account.example.com and download my latest invoice using secret 'example-login'
```

## Limitations

The browser profiles share these boundaries:

- **Credential protection**: `/browse` runs anonymously without access to passwords.
  `/browse_authenticated` can autofill credentials via Keychute: tool responses do not return secret
  values and accessibility snapshots mask password controls. Read-back protection against an
  approved site is best effort, as an approved page receives the password and could echo it.
- **No downloads to your device** — files downloaded go to the assistant's environment.
- **Cannot bypass CAPTCHAs or paywalls automatically** — the assistant can offer a human browser
  handoff link for interactive challenges, but cannot solve them autonomously.
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
- If the page changed under the assistant, the action fails and comes back with a fresh view of the
  page; ask it to try again from there.
- Ask the assistant to take a snapshot/screenshot first to check the page state.

### Session Timeout

- Long-running browser sessions may time out. Break complex tasks into smaller requests.
- Start a new `/browse` session if needed.

## Related Features

- **[Research and Web Browsing](research-and-browsing.md)** — when to reach for `/browse` at all
- **[User Guide](USER_GUIDE.md)** — index of every topic guide
- **[Scripting Guide](scripting.md)** — automate tasks with scripts
- **Document Ingestion** — save web content: "Save this page for later: [URL]"
