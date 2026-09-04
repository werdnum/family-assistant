# Browser Refs: Resolved Against the Live Page

## Status

Proposed.

## Problem

The semantic DOM tools (`browser_click`, `browser_fill`, `browser_select`) target elements by a
short ref (`e12`) taken from the most recent snapshot. Resolution happens in two places that do not
agree about what "valid" means:

- **In the page.** The snapshot script stamps each interesting element with `data-fa-ref="eN"`, and
  every action is sent to the browser as the selector `[data-fa-ref="eN"]`. A ref works exactly when
  a stamped element is still in the DOM.
- **In the client.** Family Assistant keeps a per-session `ref_cache` dict of the refs seen in the
  last snapshot and refuses, before any request reaches the browser, to act on a ref that is not in
  it. The dict is cleared without a replacement snapshot by `browser_exec` (unconditionally, even
  for a pure read), by every visual-profile screenshot, and on session loss.

The client-side dict fails where the page would have succeeded. Every one of the 15 `Unknown ref`
failures logged over the last month shows `Known refs: []`: the cache had been wiped by one of the
side channels above while the stamped elements were still on the page. The model cannot anticipate
this, because `browser_exec` returns neither a snapshot nor any notice that refs were invalidated,
and no tool description mentions it. Each failure costs a wasted round trip plus a remedial
`browser_snapshot`, and the extra full-page snapshot is replayed into every later call of the
session.

The dict also adds nothing the page does not already provide. Between two snapshots the attribute
stays on the node it was stamped on, so within that window a ref is the element it was issued for or
nothing. Refs are only reused when a later snapshot renumbers, and every action returns a fresh
snapshot, so the model always holds the newest numbering.

## Prior art

Source-verified against the current trees of each project:

- **Playwright MCP** resolves `eN` inside the page against the last snapshot and requires the node
  to still be connected. A miss is a hard error ("Ref e12 not found in the current page snapshot.
  Try capturing new snapshot."), with no retry. Refs restart per document. Mutating tools include a
  fresh snapshot in their own response.
- **Chrome DevTools MCP** once threw an explicit "this uid is coming from a stale snapshot" error;
  that bookkeeping was removed and a miss is now simply "not found" or "no longer exists".
  Post-action snapshots became opt-in after a single click was reported to cost 211k tokens.
- **browser-use** returns a soft "index not available, page may have changed" observation and
  dropped its per-action DOM re-hash in favour of cheap URL and focus-target guards.
- **Stagehand** is the only harness that auto-retries (`selfHeal`), by re-running LLM inference,
  because its refs are xpaths.
- **Coordinate-based computer-use models** have no ref concept.

The convergent design: staleness is decided by the live page at resolution time, misses fail loudly
and cheaply, actions go through a locator, and nobody keeps a separate client-side allowlist.

## Design

### The contract

A ref is valid until the model receives its next snapshot, and every action returns one. That is the
whole promise, and it is what the tool descriptions and the `/browse` system prompt will state. Refs
stay positional and are renumbered per snapshot, as today.

### Staleness is decided by the page

The client-side `ref_cache`, `clear_refs`, and every call to them are deleted. Client-side
resolution becomes a syntactic check that the ref is well-formed and a string transform to the
selector. `browser_exec` and the visual profile stop touching ref state; there is nothing for them
to invalidate.

Before acting, the backend runs the walker's own eligibility predicate on the stamped node in the
page: a ref resolves exactly when a snapshot taken at that moment would list a node under it. A
removed node, a hidden node, or a node whose role or name changed fails immediately with a specific
message ("ref e35 is no longer on the page as snapshotted; the page has changed since the last
snapshot") rather than waiting out Playwright's actionability timeout. The walker and the resolver
share that predicate as one in-page function so they cannot drift. For the remote backend this check
lives in browser-server's click, type and select handlers; the local backend does the same in its
own evaluate step.

### A miss returns the snapshot it would otherwise force

When an action fails because the ref is gone, the tool result carries the error together with a
fresh snapshot of the current page, so the model retargets on its next call instead of spending a
round trip on `browser_snapshot`. There is no automatic retry: choosing a replacement element is an
inference the model should make with the page in front of it.

## Deliberate simplifications

- **Refs are not identity-stable across snapshots.** A ref from a snapshot older than the model's
  latest tool result may, after renumbering, resolve to a different element. The model is never
  without a newer snapshot when that is true, and this is the residual Playwright MCP ships with. An
  earlier draft promised refs that survive session replacement and process restarts; that promise
  needed a conversation-scoped allocator, a generation fence and node-identity records in the page,
  and was withdrawn because nothing in the observed failures needed it. If within-document stability
  is wanted later, Playwright's in-page approach (reuse a ref when role and name match, count on the
  document) adds it without any client-side state.
- **Actions go through a locator, not a bound element handle.** A page that replaces a stamped node
  between the pre-action check and the action, or that clones one with its attribute intact, can
  land the action on the replacement. Every surveyed harness accepts this race; it is rare enough
  for reasonable rather than ideal behaviour.
- **No diff or abbreviated snapshot mode.** No surveyed harness produces one; the proven lever for
  post-action token cost is returning less, which the existing `query` filter already provides.
- **No refs for elements added since the last snapshot.** They are unaddressable until the next
  snapshot, the same contract as today.

## Work plan

1. **browser-server: pre-action eligibility check.** Outcome: click, type_text and select return a
   distinct error for a ref whose node would no longer be listed by a snapshot, using the walker's
   predicate. Verified by the service's own tests: an action on a removed ref, a hidden ref and a
   relabelled ref each fails fast with the specific error, and an action on an unchanged ref
   succeeds.
2. **Family Assistant: delete the ref cache; mirror the check; attach snapshots to misses.**
   Outcome: `ref_cache` and `clear_refs` are gone from the backend protocol, the local session and
   the computer-use tools; the local backend performs the same pre-action check; a miss returns
   error plus snapshot; tool descriptions and the `/browse` prompt state the contract; the browser
   automation user guide is updated. Verified by the existing functional tests rewritten for the new
   contract, including click-after-`browser_exec` succeeding and click-after-removal returning the
   error with a snapshot.

Milestone 1 merges before milestone 2 starts, because the remote backend's behaviour is what the
functional tests in milestone 2 assert against.
