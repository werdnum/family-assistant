# Browser Refs: One Ref, One Node

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

The dict also does not prevent the failure that matters most. Refs are positional and every snapshot
renumbers from `e1`, so the same ref names different nodes on different pages, and on the same page
before and after a re-render. A ref the model copies from any snapshot but its latest can therefore
pass the dict and land on an unrelated element. That is silent: no error, a wrong click on a live
site, and a model that believes it did what it asked. Two ordinary paths produce it. The model's
context holds every snapshot of the session, so after navigation it can pick `e12` from the previous
page's snapshot. And when the model emits two DOM actions in one response, the loop runs them
concurrently, so the first action's post-action snapshot renumbers the page before the second
resolves its ref.

## Prior art

Source-verified against the current trees of each project:

- **Playwright MCP** resolves `eN` inside the page against the last snapshot and requires the node
  to still be connected. Refs are cached on the DOM node and reused across snapshots while the
  element's role and name are unchanged; the counter lives with the document, so numbering restarts
  on navigation. A miss is a hard error ("Ref e12 not found in the current page snapshot. Try
  capturing new snapshot."), with no retry. Mutating tools include a fresh snapshot in their own
  response.
- **Chrome DevTools MCP** keys uids to the backend DOM node and reuses them across snapshots; new
  uids are prefixed with a monotonically increasing snapshot id, so a uid is never reused for a
  different node within a session. It once threw an explicit "stale snapshot" error and removed that
  bookkeeping once uids were unique. Post-action snapshots became opt-in after a single click was
  reported to cost 211k tokens.
- **browser-use** returns a soft "index not available, page may have changed" observation and
  dropped its per-action DOM re-hash in favour of cheap URL and focus-target guards.
- **Stagehand** is the only harness that auto-retries (`selfHeal`), by re-running LLM inference,
  because its refs are xpaths.
- **Coordinate-based computer-use models** have no ref concept.

The convergent design: ref identity lives in the page, staleness is decided by the live page at
resolution time, misses fail loudly and cheaply, actions go through a locator, and nobody keeps a
separate client-side allowlist. Chrome DevTools MCP's session-unique uids are the one property the
others lack, and it is the one that turns a wrong click into an error.

## Design

### The contract

A ref names one node, and is never issued for another node in the same conversation. It stays valid
for as long as that node is on the page with the role and accessible name it was snapshotted with.
That is the whole promise, and it is what the tool descriptions and the `/browse` system prompt will
state: refs survive snapshots and actions, any ref the model has ever been shown either targets the
node it was issued for or fails, and a failure returns a fresh snapshot to retarget from.

### Refs are reused in the page and numbered once per conversation

The snapshot script stops stripping and renumbering. A node that already carries a ref keeps it when
its role and name are unchanged; anything else is stamped with a fresh number. Fresh numbers come
from a counter held in the conversation's persistent browser state, the object that already outlives
individual tool calls on both the local and the remote path. The counter is passed into the walker
with every snapshot and read back from the result, so it advances across documents and a number is
never issued twice. It is seeded randomly when that state is created, so numbers issued before a
process restart do not collide with numbers issued after it. The seed range only has to make such a
collision negligible; the implementing PR chooses it.

This is one integer of client state and no identity bookkeeping. A ref from an earlier page, an
earlier turn, or a concurrently issued sibling finds no node on the current page, and the page says
so.

### Staleness is decided by the page

The client-side `ref_cache`, `clear_refs`, and every call to them are deleted. Client-side
resolution becomes a syntactic check that the ref is well-formed and a string transform to the
selector. `browser_exec` and the visual profile stop touching ref state; there is nothing for them
to invalidate.

Before acting, the backend runs the walker's own eligibility predicate on the stamped node in the
page: a ref resolves exactly when a snapshot taken at that moment would list that node under it. A
removed node, a hidden node, a node whose role or name changed, and a ref from a different document
all fail immediately with a specific message ("ref e35 is no longer on the page as snapshotted; the
page has changed since the last snapshot") rather than waiting out Playwright's actionability
timeout. The walker and the resolver share that predicate as one in-page function so they cannot
drift. For the remote backend this check lives in browser-server's click, type and select handlers;
the local backend does the same in its own evaluate step.

### Browser operations run in order, and a batch hands back one set of refs

The tool loop runs a response's tool calls concurrently. Every browser operation for a conversation,
whether or not it consumes a ref, passes through one chokepoint in the conversation's persistent
browser state and runs in the order the model issued it, each against the page as the previous one
left it. Unique numbering makes this safe rather than merely ordered: a sibling whose predecessor
navigated fails on the new page instead of resolving.

A batch can pass through more than one document, so its results can carry snapshots of several. The
chokepoint hands back only the last snapshot-bearing result with refs, and the earlier ones without
refs and with a line saying the page has since moved on. Those refs would fail anyway; stripping
them keeps the model from being shown a ref it cannot use.

### Every snapshot-returning tool takes the same filter

The post-action snapshot is where the token cost of browsing lives, and today only `browser_open`
and `browser_snapshot` accept the `query` filter; a click, fill or select on a large page always
returns the whole tree. The filter moves to the shared snapshot path so every tool that returns a
snapshot accepts it, actions included. A filtered snapshot still carries refs, and those refs are
the same ones an unfiltered snapshot would show, so filtering costs nothing in correctness. The
default stays unfiltered: the model asks for less when it knows what it is looking for.

### A miss returns the snapshot it would otherwise force

When an action fails because the ref does not resolve, the tool result carries the error together
with a fresh snapshot of the current page, so the model retargets on its next call instead of
spending a round trip on `browser_snapshot`. There is no automatic retry: choosing a replacement
element is an inference the model should make with the page in front of it.

## Deliberate simplifications

- **Uniqueness across a process restart is probabilistic.** A random seed, not a persisted allocator
  or a generation fence, keeps post-restart numbers away from pre-restart ones. An earlier draft
  carried document identity across the browser-server boundary and a fence that compared it at the
  chokepoint; unique numbering makes both redundant, because a ref from another document simply
  finds nothing.
- **Identity is the stamped attribute plus role and name, not the node object.** A page that clones
  a stamped node with its attribute intact produces a look-alike the resolver cannot tell apart.
  Every surveyed harness except Playwright accepts this; it is rare enough for reasonable rather
  than ideal behaviour.
- **Actions go through a locator, not a bound element handle.** A page that replaces a stamped node
  between the pre-action check and the action can land the action on the replacement. Every surveyed
  harness accepts this race; it is rare enough for reasonable rather than ideal behaviour.
- **Ref numbers grow.** A long session reaches five or six digits, a token or so more per ref than
  today. That is the price of a ref that can never silently mean something else, and it is the trade
  Chrome DevTools MCP makes.
- **No diff or abbreviated snapshot mode.** No surveyed harness produces one; the proven lever for
  post-action token cost is returning less, which the `query` filter provides once every
  snapshot-returning tool accepts it.
- **No refs for elements added since the last snapshot.** They are unaddressable until the next
  snapshot, the same contract as today.

## Work plan

1. **browser-server: in-page ref reuse from a caller-supplied counter, and pre-action eligibility
   check.** Outcome: the snapshot command keeps refs on unchanged nodes, numbers new ones from the
   counter the caller passes in and reports the advanced counter; click, type_text and select return
   a distinct error for a ref whose node would no longer be listed by a snapshot, using the walker's
   predicate. Verified by the service's own tests: two snapshots of an unchanged page yield
   identical refs, a node inserted before an existing one does not renumber it, a relabelled node
   gets a new ref, numbering continues across navigation and a same-URL reload, and an action on a
   removed, hidden, relabelled or previous-document ref fails fast with the specific error.
2. **Family Assistant: delete the ref cache; hold the counter; mirror the walker; serialise
   operations; attach snapshots to misses.** Outcome: `ref_cache` and `clear_refs` are gone from the
   backend protocol, the local session and the computer-use tools; the persistent browser state
   holds the randomly seeded counter and threads it through every snapshot on both paths; the local
   walker matches browser-server's and the local backend performs the same pre-action check; every
   browser operation for one conversation runs one at a time in issue order, and a batch hands back
   refs only in its last snapshot; a miss returns error plus snapshot; `browser_click`,
   `browser_fill` and `browser_select` accept the same `query` filter as `browser_snapshot`; tool
   descriptions and the `/browse` prompt state the contract; the browser automation user guide is
   updated. Verified by the existing functional tests rewritten for the new contract, including
   click-after-`browser_exec` succeeding when the script did not navigate, click-after-removal
   returning the error with a snapshot, a ref copied from the previous page's snapshot failing with
   the current page's snapshot, two ref actions issued from one snapshot both landing on their own
   nodes when run as a batch, a batched action after a navigating sibling failing rather than
   acting, and a batch that navigates twice handing back refs only in its final snapshot, and a
   filtered post-click snapshot containing only matching branches with refs that resolve.

Milestone 1 merges before milestone 2 starts, because the remote backend's behaviour is what the
functional tests in milestone 2 assert against.
