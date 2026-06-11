# iOS App Navigation Redesign

## Problem

The current iOS navigation is incoherent. The app has three top-level destinations — Chat, Notes,
and a generic "Web App" — and each one carries its own ad-hoc set of cross-links in its toolbar:

- The Chat list pane links to Notes and Web App in its top-leading toolbar.
- The Chat detail pane has no way out (no Notes/Web/Settings links).
- The Notes screen links only back to Chat.
- The WKWebView fallback has a custom bottom toolbar with back/forward/reload + Notes — but no way
  to reach Chat.
- `AppSettingsMenu` (sign-out, notifications toggle) is duplicated in three different toolbars.

Two underlying problems:

1. **No consistent way to switch top-level sections.** Every screen invents its own jump links, and
   they aren't symmetric.
2. **"Web App" is a nerdview destination.** It exposes an implementation detail (this part is in a
   WKWebView) instead of a feature. Users care about Documents and Voice — not about which renderer
   happens to draw them today. And because `/chat` exists in both forms, the user can end up in the
   web chat even though we already have a native chat with parity.

## Proposal

Replace the ad-hoc top-bar cross-links with a single `TabView` whose tabs are **features**, not
implementations. Each tab is a `NavigationStack` that renders natively where we have a native screen
and falls back to a focused `WKWebView` at the appropriate path where we don't — same tab label and
icon in both cases.

### Tab set (initial)

| Tab       | Implementation          | Path / source       |
| --------- | ----------------------- | ------------------- |
| Chat      | Native (existing)       | `ChatRootView`      |
| Notes     | Native (existing)       | `NotesRootView`     |
| Documents | WKWebView               | `/documents/`       |
| More      | Native list → WKWebView | Voice, Events, etc. |

Four tabs, not five: only Chat, Notes, and Documents are genuine user-facing destinations worth a
primary slot today. Everything else — including Events (a debugging/internal surface) and Voice (not
native yet) — lives under More. This deliberately leaves the 5th tab-bar slot free for the first
long-tail destination that earns promotion (most likely Voice, once it ships natively).

The **More** tab is a native list of the long-tail destinations drawn from the canonical web nav
(`frontend/src/shared/navigation.ts`), each of which opens its corresponding web page inside the
same tab's `NavigationStack`. Settings (sign-out, notifications toggle, theme) also lives in More —
one place, no longer duplicated. The exact destination set and paths are enumerated in
[More tab destination map](#more-tab-destination-map) below.

### What goes away

- The "Web App" top-level route. The generic web-fallback wrapper is gone; web pages always live
  inside a feature-named tab.
- `WebViewToolbar` (back/forward/reload at the bottom). System-standard back via the
  `NavigationStack` replaces it. Pull-to-refresh in the WKWebView replaces reload.
- The duplicated `AppSettingsMenu` placements in Chat list, Notes, and Web toolbars.
- The native side never opens `/chat` in a WKWebView. Any deep-link/notification path of `/chat`
  routes to the native `Chat` tab instead.

### Cross-cutting behavior

- **Deep links / notifications**: the `resolve(_:relativeTo:)` resolver (see
  [Router design](#router-design)) keeps mapping `/chat` and `/notes` paths to their native screens;
  `/documents/*` routes to the Documents tab and everything else (Events, Voice, …) routes to the
  More tab, pushing the WKWebView at the resolved path.
- **iPad**: `TabView` on iPad adapts to a sidebar in iOS 18 — acceptable. We lose the current
  `NavigationSplitView` sidebar in Chat. If we want a chat conversation list visible alongside the
  thread on iPad, that's a follow-up inside the Chat tab using `NavigationSplitView` nested within
  the tab.
- **State**: each tab keeps independent navigation state across tab switches. The router enum
  `AppRoute` collapses from `chat | web(path) | notes(route)` into per-tab routes; the global switch
  becomes the selected-tab index.

## Resolved decisions

These were the open questions; they are now decided so implementation can proceed. Each notes the
trigger that would reopen it.

### Voice stays under More for now (native promotion later)

The primary tab set is **Chat / Notes / Documents / More**. Voice lives in the More list at `/voice`
for this change.

- **Why**: Voice is still a WKWebView surface today; a top-level tab should be a real native
  feature, not a prominent slot for a web page. Voice *will* go native — but later, as its own
  effort.
- **Reopen if** (and the plan is to): Voice ships as a native screen. At that point promote it into
  the free 5th tab-bar slot we deliberately left open — no demotion of another tab required, so
  there's no nav churn.

### Events is a debugging surface — it lives under More, not a tab

Events is effectively an internal/debugging UI, so it does **not** earn a primary tab. It appears as
a row in the More list that opens `/events` in a `WebDestinationView`. We also do **not** build a
unified native Calendar in this change.

- **Why**: A primary tab is reserved for genuine user-facing features. Demoting Events to More keeps
  the tab bar honest and frees a slot. A native calendar (events + task reminders) is a separate
  epic and out of scope for a navigation redesign.
- **Reopen if**: We build a real user-facing native calendar — *that* could earn a top-level tab,
  with the debugging `/events` page remaining a More row.

### Settings stays in one place (More), not a per-tab gear

`AppSettingsMenu` collapses to a **single Settings row inside the More tab**. We do not add a gear
icon to every tab's trailing toolbar.

- **Why**: The duplication of `AppSettingsMenu` across three toolbars is exactly the incoherence
  this redesign removes; re-adding it to four toolbars would reintroduce the same problem in a new
  shape. Settings actions (sign-out, notifications toggle, theme) are infrequent and the More tab is
  always one tap away in the tab bar.
- **Reopen if**: User testing shows people can't find Settings — the cheapest mitigation is a gear
  in the Chat tab only (the most-used screen), not all four.

## Router design

`AppRoute` collapses from a single global enum into a tab selection plus independent per-tab
navigation state. `AppRouter` remains the single `@Observable` source of truth.

```swift
enum AppTab: String, CaseIterable, Hashable {
    case chat, notes, documents, more
}

@Observable
final class AppRouter {
    var selectedTab: AppTab = .chat

    // Chat tab: the existing conversation/prompt selection (not a stack).
    var chatSelection: ChatRoute = .init(conversationID: nil, initialPrompt: nil)

    // Each remaining tab owns an independent NavigationStack path.
    var notesPath: [NotesRoute] = []
    var documentsPath: [WebRoute] = []
    var morePath: [MoreRoute] = []
}
```

Supporting route types:

- `ChatRoute` becomes a small struct (`conversationID`, `initialPrompt`) rather than the
  factory-only enum it is today; its existing `route(for:relativeTo:)` parser is retained as a
  static factory.
- `NotesRoute` is unchanged (already `Hashable`); it now drives the Notes tab's `NavigationStack`
  path instead of the global route.
- `WebRoute` is a new `Hashable` value wrapping `path: String` (and an optional display title) used
  by the Documents and More stacks and `WebDestinationView`.
- `MoreRoute` is a new `Hashable` enum: `.web(WebRoute)` for the long-tail destinations and
  `.settings` for the native Settings screen.

### URL → (tab, route) resolution

`openNativeURL` is replaced by a single pure resolver that every deep-link/notification path flows
through. It is exhaustive — there is no "unmatched" fallthrough, because the More tab catches
everything not owned by another tab. This kills the old "drop into a generic web view" path,
satisfying the design goal that `/chat` (and every other path) always resolves to a feature tab.

```swift
func resolve(_ url: URL, relativeTo baseURL: URL) -> (tab: AppTab, apply: (AppRouter) -> Void)? {
    guard url.matchesOrigin(of: baseURL) else { return nil }  // foreign origin → open in Safari
    let path = url.normalizedPath   // percent-encoded path, no trailing slash

    if let chat = ChatRoute.route(for: url, relativeTo: baseURL) {
        return (.chat, { $0.chatSelection = chat })
    }
    if let notes = NotesRoute.route(for: url, relativeTo: baseURL) {
        return (.notes, { $0.notesPath = [notes] })
    }
    if path == "/documents" || path.hasPrefix("/documents/") {
        // /documents/ is the tab root; deeper paths push onto its stack.
        let isRoot = (path == "/documents" || path == "/documents/")
        return (.documents, { $0.documentsPath = isRoot ? [] : [WebRoute(path: url.pathAndQuery)] })
    }
    // Everything else — Events, Voice, History, Automations, Tools, etc. — is owned by More.
    return (.more, { $0.morePath = [.web(WebRoute(path: url.pathAndQuery))] })
}
```

Notes on resolution:

- `/chat` and `/notes/*` route to their native screens exactly as today — the native side never
  opens `/chat` in a WKWebView.
- A bare `/documents/` selects the Documents tab at its root (which *is* the `/documents/` web page)
  rather than pushing a duplicate; deeper `/documents/...` paths push a `WebRoute`.
- `/events` (and every other long-tail path) resolves to the More tab, pushing the matching
  `WebRoute` onto its stack.
- Selecting a tab via `resolve` sets that tab's path *and* `selectedTab`; it does not disturb the
  other tabs' stacks, preserving the "independent state per tab" goal.

### In-tab web navigation belongs to the web view

A web-backed tab is a browser: in-page navigation (real link clicks *and* React Router
`history.pushState`) is owned by the WKWebView itself, with the back/forward swipe gesture and
pull-to-refresh providing back/reload. The native stack is **not** a mirror of the web view's
history — it is only for switching tabs and for deep-link landing.

This matters because the link-interception bridge usually runs *after* the SPA has already navigated
the current web view (`pushState` fires first). Pushing a native `WebRoute` in that case would stack
a second web view over a now-mutated root, so native Back would reveal the wrong page. So
`followWebLink(_:from:relativeTo:)` leaves same-tab destinations to the web view (returns `false`,
letting `WebViewContainer` proceed) and only switches tabs for cross-tab links (returns `true` after
`navigate`). Deep links still push a single landing entry via `navigate` (a freshly loaded web view,
not a `pushState` mutation), so `/documents/123` opens over the list root with a working Back.

## More tab destination map

The More list is generated from the canonical web nav in `frontend/src/shared/navigation.ts` (minus
the destinations promoted to their own tabs). Native rows route natively; web rows push a
`WebDestinationView` at the listed path within the More tab's stack.

| Section       | Row         | Destination                      |
| ------------- | ----------- | -------------------------------- |
| Data          | Context     | web `/context`                   |
| Documents     | Upload      | web `/documents/upload`          |
| Documents     | Search      | web `/vector-search`             |
| Communication | Voice       | web `/voice`                     |
| Communication | History     | web `/history`                   |
| Automation    | Automations | web `/automations`               |
| Automation    | Events      | web `/events`                    |
| Internal      | Tools       | web `/tools`                     |
| Internal      | Task Queue  | web `/tasks`                     |
| Internal      | Error Logs  | web `/errors`                    |
| Help          | Help        | web `/docs/`                     |
| Help          | About       | web `/about`                     |
| —             | Settings    | native `AppSettingsMenu` content |

Notes (`/notes`), Chat (`/chat`), and the Documents list (`/documents/`) are intentionally absent —
they are top-level tabs. Keep this table in sync with `navigation.ts`; a divergence test is
described under [Testing strategy](#testing-strategy).

## Migration milestones

Each milestone is independently testable and leaves the app in a shippable state.

### M1 — Introduce the tab shell behind the existing screens

- Add `AppTab`, `WebRoute`, `MoreRoute`, and rework `AppRouter` to the per-tab state above. Provide
  temporary shims (`openWebPath`, `openChat`, `openNotesList`) that map onto the new state so
  nothing else breaks yet.
- Add `RootTabView` with the four tabs. Chat and Notes tabs wrap the *existing* `ChatRootView` /
  `NotesRootView`; Documents/More render placeholders.
- Swap `ContentView` to render `RootTabView`. Keep `WebViewContainer` and `WebViewToolbar` in the
  tree for now (used by the placeholder web tabs).
- **Done when**: app launches into a tab bar, Chat and Notes work as before, build + existing UI
  tests pass.

### M2 — WebDestinationView and the real Documents tab

- Add `WebDestinationView` (thin wrapper over the reusable parts of `WebViewContainer`: WKWebView,
  auth, pull-to-refresh, same-origin link interception, navigation title). Internal-link taps push a
  `WebRoute` onto the *current tab's* stack instead of calling the old global router.
- Documents tab root = `WebDestinationView(/documents/)`.
- **Done when**: Documents renders its web page, in-page links navigate within the tab's web view
  (back/forward swipe + pull-to-refresh), and cross-tab links switch tabs; no bottom toolbar.
  (`WebDestinationView` is then reused by every More web row in M3.)

### M3 — More tab (native list + Settings)

- Build `MoreTabView`: the destination map above as a native `List`, each web row pushing a
  `WebRoute`, plus a native Settings row hosting the `AppSettingsMenu` content.
- **Done when**: every long-tail destination is reachable from More and renders in-tab; Settings
  actions (sign-out, notifications toggle) work from the single new location.

### M4 — Remove the old cross-links, toolbar, and global route

- Delete `WebViewToolbar` and the generic `case .web(...)` plumbing.
- Remove the Notes/Web App cross-link buttons from `ChatRootView`'s toolbar and the "Back to Chat"
  button + duplicated `AppSettingsMenu` from `NotesRootView`.
- Delete the temporary shims from M1 and the old `AppRoute` enum.
- **Done when**: no duplicated `AppSettingsMenu`, no `WebViewToolbar`, grep confirms `AppRoute` and
  `case .web` are gone.

### M5 — Deep-link / notification routing into tabs

- Route `notificationManager.pendingNavigationPath` through the new `resolve(_:relativeTo:)`: set
  `selectedTab` and apply the per-tab route. Foreign origins still open in Safari.
- **Done when**: a `/chat?conversation_id=…` notification opens the native Chat tab on that
  conversation; a `/documents/123` link opens the Documents tab pushed to that doc; a `/voice` link
  opens More → Voice; cold-launch and warm-launch both honor the pending path.

## Testing strategy

- **Unit (resolver)**: table-driven tests over `resolve(_:relativeTo:)` covering `/chat`,
  `/chat?conversation_id=…&q=…`, `/notes`, `/notes/edit/{title}`, `/documents/` (→ Documents root),
  `/documents/123` (→ Documents push), `/events` and each other More path (→ More push), a
  foreign-origin URL (→ nil), and an unknown path (→ More). This is the highest-value, fastest
  coverage and should land with M1/M5.
- **Nav divergence guard**: a test asserting the More destination map matches `navigation.ts` minus
  the promoted tabs, so a new web nav entry can't silently go missing from iOS. (Mechanism: check in
  a small JSON snapshot of the expected destinations and compare; or a comment-linked manual
  checklist if a cross-language test is too heavy — decide during M3.)
- **UI tests**: extend the existing native UI tests to (1) switch between all four tabs, (2) verify
  each tab preserves its own navigation depth across a tab switch, (3) reach Settings from More and
  perform sign-out, (4) follow a deep link into Documents and use system back.
- Keep the existing chat/notes UI tests green throughout — M1 deliberately preserves those screens
  unchanged.

## Files touched

| File                                      | Change                                                                                                                                                                       |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AppRouting.swift`                        | Rework `AppRouter` to per-tab state; add `AppTab`, `WebRoute`, `MoreRoute`; convert `ChatRoute` to a struct + factory; add `resolve(_:relativeTo:)`; remove `AppRoute` (M4). |
| `ContentView.swift`                       | Render `RootTabView`; move pending-path handling onto `resolve`.                                                                                                             |
| `RootTabView.swift` *(new)*               | The four-tab `TabView`, each a `NavigationStack` bound to its tab path.                                                                                                      |
| `WebDestinationView.swift` *(new)*        | Thin WKWebView wrapper (auth, refresh, link interception, title).                                                                                                            |
| `MoreTabView.swift` *(new)*               | Native destination list + Settings row.                                                                                                                                      |
| `WebView/WebViewContainer.swift`          | Extract reusable core for `WebDestinationView`.                                                                                                                              |
| `WebView/WebViewToolbar.swift`            | **Delete** (M4).                                                                                                                                                             |
| `Chat/ChatViews.swift`                    | Remove Notes/Web App cross-links and `AppSettingsMenu` from toolbar (M4).                                                                                                    |
| `Notes/NotesRootView.swift`               | Remove "Back to Chat" + `AppSettingsMenu`; bind to `notesPath` stack (M4).                                                                                                   |
| `Notifications/NotificationManager.swift` | Pending-path handling routes via `resolve` into tabs (M5).                                                                                                                   |
