# iOS Chat Thread Layout Watchdog Crash

## Status

M1–M3 shipped (PR #920, 2026-06-18). M4 (suspend-watchdog recurrence) shipped in PR #947. M5 (PR
#955) bounded per-message markdown layout after a **foreground** recurrence on build 23. M6 added
after a **foreground** recurrence on build 36 traced to the auto-follow scroll yanking a user who
had scrolled up. M7 covers the follow-up build 36 recurrence where the user manually scrolled while
waiting for a streamed reply; the remaining hot path was LazyStack placement itself. M8 covers a
build 39 background recurrence where the heavy thread itself observed `scenePhase`, invalidating the
bounded eager stack during the scene update.

## Summary

The TestFlight build is being killed by the iOS watchdog (`0x8BADF00D`, `EXC_CRASH`/`SIGKILL`)
because the main thread spends more than the allowed wall-clock budget inside a single SwiftUI
layout pass of the chat thread. The hang is in `ChatViews.swift` — the
`ScrollView { LazyVStack { ForEach(messages) { MessageBubble } } }` tree and the nested
`MessageBubble` / `NativeMarkdownView` stacks. It reproduces both in the background (where the
`scene-update` watchdog budget is ~10 s) and the foreground.

This document records the crash analysis and a three-milestone fix.

## Evidence

Five `.ips` reports (`scratch/FamilyAssistant-2026-06-13…` → `…2026-06-15…`) are all the same crash
family:

| Report                | State          | Watchdog event | Top app frame                                              |
| --------------------- | -------------- | -------------- | ---------------------------------------------------------- |
| 06-13-221634 (×2)     | Background     | process-exit   | `StyledTextLayoutEngine.sizeThatFits` / `_ProposedSize.==` |
| 06-15-003356          | **Foreground** | process-exit   | `UnaryLayoutEngine.explicitAlignment`                      |
| 06-15-004008          | Background     | scene-update   | `LazyLayoutViewCache.updateItemPhase`                      |
| 06-15-213321 (latest) | Background     | scene-update   | `ViewLayoutEngine.explicitAlignment`                       |

Latest report key fields:

- `exception`: `EXC_CRASH` / `SIGKILL`, code `0x0`, killer namespace `FRONTBOARD`.
- `termination`:
  `scene-update watchdog transgression: ... exhausted real (wall clock) time allowance of 10.00 seconds`.
- `procRole: Non UI`, `ProcessVisibility: Background`, `WatchdogVisibility: Background`.
- Triggered thread is `com.apple.main-thread`; the entire stack is recursive SwiftUI layout:
  `ViewLayoutEngine.explicitAlignment` → `StackLayout` → `LazyStack.measureEstimates` →
  `ForEachState.forEachItem` → `ScrollViewLayoutComputer.sizeThatFits`.

This is a **hang**, not a memory or code-signing crash. Application CPU time was only ~9.9 s of the
elapsed window, consistent with the layout engine churning proposals rather than a tight infinite
loop.

## Root cause

Two compounding factors:

1. **The chat thread layout is too expensive for large / complex threads.**

   - `MessageBubble` (`ChatViews.swift:249`) sets competing flexible-width proposals: inner content
     uses `.frame(maxWidth: .infinity, alignment: .leading)` *inside* a `.frame(maxWidth: 680)`
     *inside* an `HStack` with `Spacer(minLength: 32)`, plus `.textSelection(.enabled)` on long
     `Text`. The layout engine iterates width proposals per bubble.
   - `MarkdownBlockView` recurses into itself for block-quotes (`:765`) and list items (`:856`),
     producing deep nested-stack trees for nested markdown.
   - The existing comments at `ChatViews.swift:277-280` and `:833-836` are scar tissue from earlier
     rounds of the same problem.

2. **Launch restores the full last thread, including offscreen/background launches.** `d5af1dca`
   ("restore last chat only if recent, else land on the list") makes `ChatViewModel.init` restore
   the last conversation, and `bootstrap()` → `loadMessages` → `renderMessages` loads the *entire*
   history with no cap (`ChatViewModel.swift:1193`). When iOS spins up the `WindowGroup` offscreen
   on a background launch (push / state restoration / snapshot), SwiftUI lays out the whole restored
   thread and overruns the ~10 s background budget.

The foreground crash (06-15-003356) confirms factor (1) is real on its own — the background watchdog
just trips first because its budget is tighter.

## Plan

Three milestones, smallest-risk first. Each is independently testable.

### M1 — Gate heavy chat rendering on `scenePhase`

Render a lightweight placeholder for `ChatThreadView`'s message list when the scene is not `.active`
(background launch / snapshot), so a background `scene-update` cannot run the expensive layout. The
full list renders on the transition to `.active`.

- Read `@Environment(\.scenePhase)` at or above `ChatThreadView` and short- circuit the
  `ScrollView`/`LazyVStack` body to a placeholder (or the existing loading affordance) while
  inactive.
- Verify state restoration still lands on the right thread once foregrounded (the selection logic in
  `ChatViewModel.init` is unchanged).

Directly addresses the four background `scene-update` / `process-exit` reports. Low risk.

### M2 — Fix the foreground layout explosion (root cause)

- **`MessageBubble`**: give the bubble a single converging width. Drop the
  `maxWidth: .infinity`-inside-`maxWidth: 680`-inside-`HStack + Spacer` pattern in favor of one
  width source (e.g. a leading/trailing alignment with a single capped frame), so each bubble sizes
  in one pass.
- **`NativeMarkdownView`**: flatten rendering. Where a block is plain paragraph/inline content,
  render a single `Text(AttributedString)` rather than a recursive `VStack`/`ForEach` tree. Keep the
  recursive path only for genuinely structural blocks (tables, nested lists/quotes).
- **`.textSelection(.enabled)`**: reconsider enabling it on very large `Text`; measure its layout
  cost and scope it down if it is a contributor.

Makes the foreground hang (06-15-003356) go away too. Larger, UI-visible change — screenshot/visual
diff before and after.

### M3 — Virtualize rendering + regression harness

- **Cap initially-realized messages.** Load the full history but only realize the most recent N
  bubbles into the view; page older bubbles in on scroll-up. Keeps the first layout bounded
  regardless of thread length.
- **Repro harness (DEBUG).** A debug-only pathological thread (very long thread; deeply nested
  markdown; wide table) reachable under a launch flag, so the hang is reproducible on demand.
- **Regression test.** A layout/measurement test (or a UI test gated behind the harness) that fails
  if the chat thread layout time regresses past a threshold.

### M4 — Suspend-watchdog recurrence (post-M1–M3)

`scratch/FamilyAssistant-2026-06-25-013649.ips` crashed on build 21 (archived 2026-06-21), which
already contains M1–M3 (PR #920, merged 2026-06-18). It is the same hang family but a **different
watchdog**:

- `termination`: FRONTBOARD `0x8BADF00D`, **"Failed to terminate gracefully after 5.0s"** — the 5 s
  `process-exit` (suspend) watchdog, not the 10 s `scene-update` one the M1–M3 reports tripped.
  `WatchdogVisibility: Background`, `procRole: Non UI`.
- Main thread: `_UIUpdateSequenceRunNext` → `_UIHostingView.beginTransaction` →
  `GraphHost.flushTransactions` → `LazyLayoutViewCache.updateItemPhases` /
  `supportsViewHierarchyPrefetching` — a LazyVStack item-phase/prefetch render transaction running
  at the moment iOS tries to suspend the app.

Root cause of the recurrence: M1 gates the whole list behind
`if scenePhase == .active { messageScrollArea } else { Color.clear }`, so every
`.active → .background` transition **unmounts the entire `LazyVStack`**, forcing a teardown
transaction (`updateItemPhases` over all realized items) exactly when the OS wants the app
quiescent. M1 fixed the offscreen-*launch* path but introduced a teardown-at-suspend path.

Fix:

- **Keep the thread mounted once it has been active.** `ChatThreadView` latches `hasMountedThread`
  true on the first `.active` phase and gates on
  `ChatViewModel.shouldRenderThread(isActive:hasMountedBefore:)` (`isActive || hasMountedBefore`).
  An offscreen launch (never active) still keeps the list out of the tree — preserving M1 — but a
  later backgrounding no longer tears it down, so no transaction is kicked at suspend.
- **Don't drive layout while inactive.** The scroll-to-latest `withAnimation` in `messageScrollArea`
  is guarded on `scenePhase == .active`, so a message landing during a background transition can't
  kick an animated layout transaction at suspend. On the next foregrounding `onAppear` lands at the
  bottom unanimated.

Decision is unit-tested (`testShouldRenderThreadKeepsListMountedOnceActive`).

### M5 — Unbounded message layout (foreground recurrence, build 23)

`scratch/FamilyAssistant-2026-06-27-192549.ips` crashed on build 23 (which already contains M1–M4):
the **10s scene-update watchdog**, ~10.1s of app CPU burned on the main thread. Reported trigger:
*sending a follow-up after a turn that used tools*, preceded by a foreground freeze. The faulting
stack is the chat ScrollView sizing its content — `ScrollViewLayoutComputer.sizeThatFits` →
`StackLayout` (`sizeChildrenIdeally` / `prioritize`) → `StyledTextLayoutEngine.sizeThatFits`.

**Corrected diagnosis (an end-to-end UI test, not a theory).** The first hypothesis — that the
completed turn's *collapsed* `ToolGroupView` was being laid out — was **disproved** by the repro
test: a collapsed `DisclosureGroup` with a huge result rendered instantly, because SwiftUI does not
lay out collapsed content. The real cause is an **unbounded always-visible markdown bubble**:
`NativeMarkdownView` built a `VStack` over *every* parsed block, so a long assistant answer (or an
expanded tool result) became thousands of nested-stack `Text` nodes that the ScrollView must size in
one main-thread pass. "After a tool turn" is incidental — tool turns just tend to produce long
answers/results.

A "large message" is not only *many blocks*. It can be one block with thousands of children (a huge
list or **wide/tall table**) or one enormous `Text` (a giant code block or an unbroken string). A
fuzz harness (below) found the wide-table case after an initial block-count-only cap missed it.

**Fix.** All rendering goes through one bounded choke point,
`MarkdownRenderBudget.renderPlan(for:pages:)`, which bounds every cost dimension:

- **Parse size** — only a `charsPerPage` (16 KB) prefix is parsed per page.
- **Structure** — a `leafBudget` (150) cap on total realized leaves (paragraphs, list items, table
  rows); high-fan-out blocks are truncated to fit, and **tables cap both rows and columns** (cells),
  since a wide table's `Grid` is super-linear.
- **Single `Text`** — any one string is clamped to `textCharCap` (2000), marked inline with an
  ellipsis (never silent).

Content beyond the budget is revealed on demand via a "Show more" control
(`accessibilityIdentifier("markdown-show-more")`) that grants another page — the per-message
analogue of the thread-level "Load earlier messages" windowing (M3).

**Second cause: animated scroll-to-bottom.** The end-to-end repro still wedged on *send* even after
the bubble was bounded. A `sample` of the wedged main thread showed the cost was not in markdown
rendering (parse/bound were cold) but in SwiftUI repeatedly re-applying and re-placing the
`LazyVStack` (`LazySubviewPlacements.placeSubviews`, `ForEachState.applyNodes`, `StackLayout`) —
i.e. the `withAnimation` scroll-to-bottom past a tall bubble re-places the lazy rows every animation
frame without settling. Opening the same thread (which scrolls *without* animation) never wedged.
Fix: `messageScrollArea` now scrolls to the newest message **without** animation. This was invisible
to the unit-level budget harness because it has no `ScrollViewReader.scrollTo`; only the end-to-end
UI test exercised it — which is why both layers exist.

**Coverage (this is the durable part).** The watchdog is a *class* of bug — any unbounded layout
subtree — so the regression guard enforces the invariant, not the one shape:

- `ChatLayoutBudgetTests.testRenderPlanStaysBoundedForEveryShape` — a fast, pure-function guard (no
  view hosting, cannot wedge) asserting the *plan* the view will display has a bounded leaf count
  and no oversized `Text`, across an adversarial matrix **and a seeded fuzzer**. This catches an
  unbounded shape in milliseconds rather than wedging a layout pass for tens of minutes.
- `ChatLayoutBudgetTests.testSingleMessageLayoutStaysBoundedAcrossShapes` /
  `testFuzzedMessagesStayUnderBudget` — the symptom-level anchors: they host the real
  `ScrollView`/`LazyVStack`/`MessageBubble` path (`ChatMessageListLayoutProbe`) and assert each
  message lays out within a wall-clock budget (1.5s) well under the 10s watchdog. Shape sizes are
  tuned so a future cap regression fails *slow* (seconds) rather than wedging the suite, since a
  synchronous main-thread layout cannot be interrupted.
- `FamilyAssistantUITests.testFollowUpAfterToolTurnStaysResponsive` — the end-to-end repro: seeds a
  tool turn with a very large answer (`web_conv_tool_heavy`), sends a follow-up, and asserts the app
  stays responsive (the original failing case).

### M6 — Auto-follow yank (foreground recurrence, build 36)

A sixth report (`scratch/FamilyAssistant-2026-07-07-090155.ips`, build 36) is the same watchdog
family (`0x8BADF00D`, `scene-update`, 10 s) but a **new trigger, not a new layout cost**. The user
was scrolling through history when a reply arrived; the app locked up. The wedged main thread is in
the **list-placement** path — `LazySubviewPlacements.placeSubviews` → `LazyStack.place` →
`StackPlacement.measureBackwards` → `_LazyLayout_Subview.lengthAndSpacing` — not a single deep
markdown subtree (the sampled per-bubble subtree is normal depth, and the per-message budget from M5
is intact). `WatchdogCPUStatistics` show only ~17 % app CPU across the window: the main thread was
not pegged in one hot loop, it was re-running placement passes that never settled.

**Root cause.** `messageScrollArea` fired `proxy.scrollTo(lastID, anchor: .bottom)` on *every*
change of the newest bubble's id, gated only on `scenePhase == .active`. When a reply lands while
the user has scrolled up (or is mid-drag), that bottom-anchored `scrollTo` forces the `LazyVStack`
to re-resolve its bottom anchor and `measureBackwards` over the visible rows while fighting the
user's scroll position — it never converges, and the 10 s scene-update watchdog kills the app. This
is the unanimated cousin of M5's second cause (animated scroll-to-bottom): M5 dropped the animation,
which fixed the *send-while-idle* case, but not *reply-arrives-while-reading-history*.

**Related case found (no separate report yet).** The voice transcript (`VoiceView.swift`) had the
*same* class of bug in a different shape: it scrolled `withAnimation` on **every streamed token**
(`onChange(of: entries.last?.text)`) — animated follow (M5 cause #2) fired at partial-transcript
frequency, a latent live-voice wedge. Two hand-rolled auto-followers, each unsafe in a different
way, with no shared invariant.

**Fix (single choke point for auto-follow).** Introduce `StickyBottomScroll` (in `ChatViews.swift`)
and route **both** the chat thread and the voice transcript through it — mirroring how
`MarkdownRenderBudget` is the single choke point for per-message layout *cost*. It enforces, in one
place:

- **Never animate the follow scroll** (kills the M5-cause-#2 shape wherever it recurs, including
  voice).
- **Only follow a passive arrival when near the bottom.** A zero-height bottom sentinel tracks "is
  the user at the bottom" (`onAppear`/`onDisappear`); a *passive* arrival (streamed reply, tool
  step, synced message; `followTrigger` = the newest bubble's id) follows only when they are near
  the bottom — so a reply never yanks a reader who scrolled up.
- **A local send always pins to the bottom, signalled explicitly.** Sending is a user-initiated
  event and must scroll into view even from scrolled-up. It can *not* be inferred from the last
  bubble's role: `sendDraft()` appends the user bubble **and** an assistant loading placeholder, so
  the newest bubble is `.assistant` right after a send. The view model bumps
  `scrollToLatestRequestID` in `sendDraft()`, and the view drives it as
  `StickyBottomScroll.forceFollowTrigger` (an unconditional, un-gated scroll). `forceFollowTrigger`
  is deliberately **not** scene-phase-gated — it only ever changes on a send (always active), and
  gating it would fire it on the `nil → value` flip when returning to the foreground, re-introducing
  the yank.

**Coverage.** `ChatViewModelTests.testSendDraftRequestsScrollToLatest…` asserts a send bumps the
force signal (the local-send case a role heuristic misses).
`ChatLayoutBudgetTests.testStickyBottomScroll*` hosts the real `StickyBottomScroll`, scrolls it up,
and asserts: a *denied* passive follow does not jump to the bottom, an *allowed* one does, and a
*force* trigger scrolls even when the gate denies (the deny/allow pair is mutually validating — the
allow case proves the follow mechanism fires, so the deny case is non-vacuous). These UIKit-hosted
tests wait on the observed scroll state via a run-loop poll (`waitUntil`), never a fixed sleep, and
wait for the initial land to settle before scrolling up so a late `onAppear` scroll can't masquerade
as a follow. The existing `testFollowUpAfterToolTurnStaysResponsive` /
`testNativeChatSendsAndStreamsResponse` UI tests continue to cover open-lands-at-bottom and
send-follows in the real app.

### M7 — Manual scroll during streaming (foreground recurrence, build 36)

`scratch/FamilyAssistant-2026-07-09-175705.ips` is another build 36 foreground `scene-update`
watchdog kill (`0x8BADF00D`). The user-visible trigger was scrolling while waiting for a reply.
Unlike M6, the chat follow trigger was already gated and only changed on newest-row identity, not on
streamed text, so the app was not firing a `scrollTo` per token.

The main thread was still in the same SwiftUI lazy-list placement family:
`_ViewList_TemporarySublistTransform.apply` → `_LazyLayout_Subviews.apply` → `LazyStack.place` /
`LazySubviewPlacements.updateValue`. That points at the remaining lazy stack itself: while the user
scrolls, the streamed tail row mutates every flush, and SwiftUI keeps recomputing lazy placements
for the bounded visible window until the scene-update watchdog kills the app.

**Fix.**

- Keep the thread-level and per-message bounds from M3/M5, but render the already-bounded visible
  window with an eager `VStack` instead of `LazyVStack`. The initial window is 30 grouped bubbles
  and "Load earlier messages" widens it by explicit user action up to a fixed 120-bubble ceiling,
  then slides that bounded window through older history with a matching "Load newer messages"
  affordance, so eager layout is bounded while avoiding the LazyStack placement path that appears in
  every recurrence and history beyond the ceiling remains reachable.
- Apply the same eager-stack choice to the voice transcript, whose streaming-tail behavior shares
  the same sticky-bottom container.
- Add `ChatLayoutBudgetTests.testStickyBottomScrollHandlesTailMutationWhileUserIsScrolledUp`, which
  hosts the real sticky scroll view, scrolls away from the bottom, mutates the tail repeatedly, and
  asserts it neither yanks to bottom nor spends watchdog-scale time in layout.

### M8 — Scene-phase environment invalidation (background recurrence, build 39)

Two distinct build 39 reports are background `scene-update` watchdog kills (`0x8BADF00D`, 10 s):
`scratch/FamilyAssistant-2026-07-15-083505.ips` and `scratch/FamilyAssistant-2026-07-15-151118.ips`.
(The file named `151118 1.ips` is byte-identical to `151118.ips`, so it is one incident rather than
a third.) The `083505` main thread is flushing an AttributeGraph view transaction; `151118` provides
the more specific sample inside Swift generic metadata instantiation from
`EnvironmentBox.update(property:phase:)` → `_DynamicPropertyBuffer.update` →
`DynamicBody.updateValue`. Neither report returns to the M7 `LazyStack` placement path.

M7 removed the unbounded lazy-placement path, but `ChatThreadView` still directly declared
`@Environment(\.scenePhase)`. Every active/background transition therefore invalidated the parent
whose body owns the eager message stack (up to 120 complex bubbles). Keeping that stack mounted
avoided M4's teardown transaction, but recomputing its dynamic-property graph during backgrounding
could still consume the full scene-update allowance.

**Fix.**

- Move `scenePhase` observation into a zero-sized `ChatScenePhaseObserver` below a mount gate. The
  observer handles the one-time foreground mount and reconnect callback, while later phase changes
  do not invalidate `ChatThreadView` or reconstruct its message closure.
- Stop encoding activity into the message list's `followTrigger`. `StickyBottomScroll` instead
  checks an injected `canFollow` closure when a passive trigger changes; production reads
  `UIApplication.shared.applicationState` without creating a SwiftUI environment dependency. The
  force-follow signal remains unconditional because it is emitted only by an active user send. If
  lifecycle suppression defers an otherwise-allowed passive follow, the shared scroll container
  preserves it and retries on `UIApplication.didBecomeActiveNotification`; the content that arrived
  in the background is therefore visible immediately on foregrounding without invalidating the heavy
  parent view.
- Cover the lifecycle suppression at the hosted scroll-view layer: an otherwise-allowed passive
  follow is denied while `canFollow` is false and leaves the scroll position untouched, then the
  same pending follow runs when the lifecycle gate reopens.

### M9 — Eager-window process-exit layout (foreground recurrence, build 39)

`scratch/FamilyAssistant-2026-07-15-163058.ips` is the third distinct July 15 incident. It is not a
background scene update: FrontBoard killed the app after it failed to terminate within the 5 s
`process-exit` allowance (`WatchdogVisibility: Foreground`). The main thread is synchronously
placing the eager message stack through `StackLayout.UnmanagedImplementation.placeChildren`,
`explicitAlignment`, and `ViewLayoutEngine.sizeThatFits`.

M7 correctly removed the repeatedly non-settling `LazyVStack` path, but its eager replacement could
grow from 30 to 120 complex bubbles after repeated "Load earlier messages" actions. A 120-bubble
tree is bounded in the mathematical sense but is still too large for synchronous placement or
teardown under the shorter process-exit watchdog.

**Fix.** Keep the eager renderer, but make its window a fixed 30 grouped bubbles. "Load earlier
messages" and "Load newer messages" slide that fixed window instead of growing it, so all history
remains reachable and SwiftUI never realizes four pages simultaneously. A view-model regression test
enforces the fixed-size paging behavior, and
`ChatLayoutBudgetTests.testMaximumThreadWindowLayoutStaysUnderExitWatchdogBudget` hosts the maximum
production window as one layout pass against a budget below the 5 s exit allowance.

## Verification

- `M1`: launch into a restored thread from background (simulate scene-update while inactive) and
  confirm no watchdog kill; confirm the thread renders on foreground.
- `M2`: build to local disk and exercise long / nested-markdown threads in the simulator; confirm
  smooth layout and no hang; visual diff of bubbles.
- `M3`: run the repro harness before/after; the regression test passes on the fixed build and fails
  on a reverted layout.
- Full iOS unit + UI test bundle (app-hosted) green before PR. Reproduce CI locally by running the
  full bundle from local disk (not the shared mount).

## Risks / notes

- M2 changes user-visible bubble layout; needs visual review.
- The background guard (M1) must not break state restoration or the scroll-to-latest behavior on
  foregrounding.
- iOS unit tests are app-hosted; the host app must not boot its real UI under XCTest. Keep the
  harness behind the existing `UITestConfiguration` gating.
