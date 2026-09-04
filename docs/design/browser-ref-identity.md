# Browser Refs: Identity-Stable, Resolved Against the Live Page

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

The client-side dict is wrong in both directions.

**It fails where the page would have succeeded.** Every one of the 15 `Unknown ref` failures logged
over the last month shows `Known refs: []`: the cache had been wiped by one of the side channels
above while the stamped elements were still on the page. The model cannot anticipate this, because
`browser_exec` returns neither a snapshot nor any notice that refs were invalidated, and no tool
description mentions it. Each failure costs a wasted round trip plus a remedial `browser_snapshot`,
and the extra full-page snapshot is replayed into every later call of the session.

**It passes where the page has changed.** Refs are positional counters, renumbered from `e1` on
every snapshot. If the page mutates on its own between snapshot and action (lazy loading, timers, a
framework re-render that drops the attribute), `e35` is still "known" and either times out in
Playwright's actionability wait or lands on a different element. For the same reason the previously
suggested recovery, re-snapshotting once and proceeding if the ref is still present, is unsafe:
`e35` exists on almost any page with 35 elements.

## Prior art

Source-verified against the current trees of each project:

- **Playwright MCP** resolves `eN` inside the page against the last snapshot map and requires the
  node to still be connected. Refs are cached on the DOM node and reused across snapshots while the
  element's role and name are unchanged. A miss is a hard error ("Ref e12 not found in the current
  page snapshot. Try capturing new snapshot."), with no retry. Mutating tools include a fresh
  snapshot in their own response.
- **Chrome DevTools MCP** keys uids to the backend DOM node and reuses the same uid across
  snapshots; new uids carry a monotonically increasing snapshot id, so ids never collide across
  navigations. Earlier versions threw an explicit "this uid is coming from a stale snapshot" error;
  that bookkeeping was removed once uids became identity-stable, and a miss is now simply "not
  found" or "no longer exists". Post-action snapshots became opt-in after a single click was
  reported to cost 211k tokens.
- **browser-use** backs indices with backend node ids, returns a soft "index not available, page may
  have changed" observation, and dropped its per-action DOM re-hash in favour of cheap URL and
  focus-target guards.
- **Stagehand** is the only harness that auto-retries (`selfHeal`), and it does so by re-running LLM
  inference because its refs are xpaths, the weakest identity of the group.
- **Coordinate-based computer-use models** have no ref concept.

The convergent design: refs are identity-stable, staleness is decided by the live page at resolution
time, misses fail loudly and cheaply, and nobody keeps a separate client-side allowlist.

## Design

### A ref names one DOM node, for the life of the conversation

The snapshot script stops stripping and renumbering. Identity is recorded in the page as a property
on the node object itself, mapping the node to its ref, role and accessible name. Cloning and
serialising a node copy attributes but not properties, so a copied element is a new element. The
`data-fa-ref` attribute remains as the lookup index that selectors use; it does not establish
identity. An element whose recorded role and name are unchanged keeps its ref; anything else is
stamped with a fresh one.

Fresh refs come from an allocator scoped to the conversation, not to the browser session: the
conversation-keyed backend object owns it and passes it into the walker on every snapshot. A browser
session can be replaced underneath a conversation (browser-server evicts or expires it and
`browser_open` transparently starts another), and a process restart replaces the backend object
itself, so a ref must also carry a generation that changes whenever the allocator is created. A ref
issued by an earlier allocator therefore never matches anything a later one issues. The wire shape
of the generation is the implementing PR's to choose.

Consequences the model can rely on, and that the tool descriptions and the `/browse` system prompt
will state: an unchanged element keeps its ref across any number of snapshots and actions; a ref
either targets the node it was issued for or nothing.

### Staleness is decided by the page

The client-side `ref_cache`, `clear_refs`, and every call to them are deleted. Client-side
resolution becomes a syntactic check that the ref is well-formed and a string transform to the
selector. `browser_exec` and the visual profile stop touching ref state; there is nothing for them
to invalidate.

Before acting, the backend resolves the ref in the page under a single rule: a ref resolves to a
node exactly when a snapshot taken at that moment would list that node under that ref. The node
found through the attribute index must be the node whose recorded identity carries the ref, and it
must still pass the walker's own eligibility predicate: connected, visible, and with the role and
name it was issued for. A removed node, a copy that inherited the attribute, a node hidden in place,
and a node relabelled since the snapshot all fail the same way, immediately and with a specific
message ("ref e35 is no longer on the page as snapshotted; the page has changed since the last
snapshot"), rather than waiting out Playwright's actionability timeout or acting on a look-alike.
The walker and the resolver share that predicate as one function so they cannot drift. Navigation
discards the page's identity records with the page, so refs from before it cannot resolve. For the
remote backend this check lives in browser-server's click, type and select handlers; the local
backend does the same in its own evaluate step.

### A miss returns the snapshot it would otherwise force

When an action fails because the ref is gone, the tool result carries the error together with a
fresh snapshot of the current page, so the model retargets on its next call instead of spending a
round trip on `browser_snapshot`. There is no automatic retry: re-resolving the same ref against a
new snapshot is what identity-stable refs make unnecessary, and choosing a replacement element is an
inference the model should make with the page in front of it.

## Deliberate simplifications

- **No diff or abbreviated snapshot mode.** No surveyed harness produces one; the proven lever for
  post-action token cost is returning less (opt-in, capped, or filtered snapshots), which the
  existing `query` filter already provides and which this change does not alter.
- **No refs for elements added since the last snapshot.** JavaScript run through `browser_exec` may
  add elements; they are unaddressable until the next snapshot, which is the same contract as today
  and matches every surveyed harness.
- **Role and name are the identity check, not deeper content.** A framework may reuse a DOM node for
  different content while keeping its role and label; that case is accepted as rare and reasonable
  rather than worth a content hash.

## Work plan

1. **browser-server: identity-stable refs and pre-action identity check.** Outcome: the snapshot
   command reuses refs for unchanged elements, allocates new ones from the allocator the client
   passes in, and click, type_text and select return a distinct error for a ref that no longer
   resolves to its node. Verified by the service's own tests: two snapshots of an unchanged page
   yield identical refs, a re-rendered element gets a new ref, an action on a removed ref fails fast
   with the specific error, a node cloned or re-serialised with its attribute intact is rejected as
   a different node, and a node hidden in place since the snapshot is rejected without waiting on
   actionability.
2. **Family Assistant: delete the ref cache; mirror the walker; attach snapshots to misses.**
   Outcome: `ref_cache` and `clear_refs` are gone from the backend protocol, the local session and
   the computer-use tools; the local walker matches browser-server's; a miss returns error plus
   snapshot; tool descriptions and the `/browse` prompt describe the new contract; the browser
   automation user guide is updated. Verified by the existing functional tests rewritten for the new
   contract, including click-after-`browser_exec` succeeding, click-after-removal returning the
   error with a snapshot, and a ref issued before a replaced browser session or a recreated backend
   never resolving afterwards.

Milestone 1 merges before milestone 2 starts, because the remote backend's behaviour is what the
functional tests in milestone 2 assert against.
