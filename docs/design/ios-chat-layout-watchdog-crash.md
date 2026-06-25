# iOS Chat Thread Layout Watchdog Crash

## Status

M1–M3 shipped (PR #920, 2026-06-18). M4 added after a suspend-watchdog recurrence
on build 21 (see "M4 — Suspend-watchdog recurrence" below).

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

`scratch/FamilyAssistant-2026-06-25-013649.ips` crashed on build 21 (archived
2026-06-21), which already contains M1–M3 (PR #920, merged 2026-06-18). It is the
same hang family but a **different watchdog**:

- `termination`: FRONTBOARD `0x8BADF00D`, **"Failed to terminate gracefully after
  5.0s"** — the 5 s `process-exit` (suspend) watchdog, not the 10 s `scene-update`
  one the M1–M3 reports tripped. `WatchdogVisibility: Background`, `procRole: Non UI`.
- Main thread: `_UIUpdateSequenceRunNext` → `_UIHostingView.beginTransaction` →
  `GraphHost.flushTransactions` → `LazyLayoutViewCache.updateItemPhases` /
  `supportsViewHierarchyPrefetching` — a LazyVStack item-phase/prefetch render
  transaction running at the moment iOS tries to suspend the app.

Root cause of the recurrence: M1 gates the whole list behind
`if scenePhase == .active { messageScrollArea } else { Color.clear }`, so every
`.active → .background` transition **unmounts the entire `LazyVStack`**, forcing a
teardown transaction (`updateItemPhases` over all realized items) exactly when the
OS wants the app quiescent. M1 fixed the offscreen-*launch* path but introduced a
teardown-at-suspend path.

Fix:

- **Keep the thread mounted once it has been active.** `ChatThreadView` latches
  `hasMountedThread` true on the first `.active` phase and gates on
  `ChatViewModel.shouldRenderThread(isActive:hasMountedBefore:)`
  (`isActive || hasMountedBefore`). An offscreen launch (never active) still keeps
  the list out of the tree — preserving M1 — but a later backgrounding no longer
  tears it down, so no transaction is kicked at suspend.
- **Don't drive layout while inactive.** The scroll-to-latest `withAnimation` in
  `messageScrollArea` is guarded on `scenePhase == .active`, so a message landing
  during a background transition can't kick an animated layout transaction at
  suspend. On the next foregrounding `onAppear` lands at the bottom unanimated.

Decision is unit-tested (`testShouldRenderThreadKeepsListMountedOnceActive`).

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
