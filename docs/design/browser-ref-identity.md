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
nothing. Refs are only reused when a later snapshot renumbers, and that renumbering has a second
victim: when the model emits two DOM actions in one response, the loop runs them concurrently, so
the first action's post-action snapshot can renumber the page before the second action resolves its
ref. The second ref is still in the dict and lands on whatever node now carries that number.

## Prior art

Source-verified against the current trees of each project:

- **Playwright MCP** resolves `eN` inside the page against the last snapshot and requires the node
  to still be connected. Refs are cached on the DOM node and reused across snapshots while the
  element's role and name are unchanged; the counter lives with the document. A miss is a hard error
  ("Ref e12 not found in the current page snapshot. Try capturing new snapshot."), with no retry.
  Mutating tools include a fresh snapshot in their own response.
- **Chrome DevTools MCP** once threw an explicit "this uid is coming from a stale snapshot" error;
  that bookkeeping was removed and a miss is now simply "not found" or "no longer exists".
  Post-action snapshots became opt-in after a single click was reported to cost 211k tokens.
- **browser-use** returns a soft "index not available, page may have changed" observation and
  dropped its per-action DOM re-hash in favour of cheap URL and focus-target guards.
- **Stagehand** is the only harness that auto-retries (`selfHeal`), by re-running LLM inference,
  because its refs are xpaths.
- **Coordinate-based computer-use models** have no ref concept.

The convergent design: ref identity lives in the page, staleness is decided by the live page at
resolution time, misses fail loudly and cheaply, actions go through a locator, and nobody keeps a
separate client-side allowlist.

## Design

### The contract

Within a document, a ref names one node for as long as that node keeps its role and accessible name;
navigation starts afresh. That is the whole promise, and it is what the tool descriptions and the
`/browse` system prompt will state: refs survive snapshots and actions, and a ref either targets the
node it was issued for or nothing.

### Refs are reused in the page

The snapshot script stops stripping and renumbering. A node that already carries a ref keeps it when
its role and name are unchanged; anything else is stamped with a fresh number from a counter that
lives on the document, so numbers are never reused within a document and die with it on navigation.
No state leaves the page: the client holds nothing, and a new document simply starts again.

### Browser actions run one at a time per conversation

The tool loop runs a response's tool calls concurrently. Browser actions on a shared tab are
serialised at one chokepoint in the backend, so two actions from one response run in order, each
against the page as the previous one left it. With refs reused in the page, the second action's ref
still names its node after the first action's snapshot.

A queued action was issued against the document the batch started on, and the model has not seen any
result yet. If an earlier action in the batch replaced that document, the queued action's ref is
meaningless on the new one, so the serialiser refuses it with the stale-ref error and the current
snapshot instead of resolving it. That is the same guard browser-use applies when a batched action
changes the page. Serialisation and this refusal live in the same chokepoint, which is what keeps
every batched action honest without the model or the page carrying anything extra.

### Staleness is decided by the page

The client-side `ref_cache`, `clear_refs`, and every call to them are deleted. Client-side
resolution becomes a syntactic check that the ref is well-formed and a string transform to the
selector. `browser_exec` and the visual profile stop touching ref state; there is nothing for them
to invalidate.

Before acting, the backend runs the walker's own eligibility predicate on the stamped node in the
page: a ref resolves exactly when a snapshot taken at that moment would list that node under it. A
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

- **Refs do not survive navigation, session replacement or a process restart.** A new document
  starts its counter again, so a ref from a previous document can name an unrelated node on the new
  one. The model always holds a snapshot of the new document before it can issue such a ref, since
  every navigating action returns one and a batched sibling is refused by the serialiser. An earlier
  draft promised refs unique across those boundaries; that needed a conversation-scoped allocator
  and a generation fence, and was withdrawn because nothing in the observed failures needed it.
- **Identity is the stamped attribute plus role and name, not the node object.** A page that clones
  a stamped node with its attribute intact produces a look-alike the resolver cannot tell apart.
  Every surveyed harness except Playwright accepts this; it is rare enough for reasonable rather
  than ideal behaviour.
- **Actions go through a locator, not a bound element handle.** A page that replaces a stamped node
  between the pre-action check and the action can land the action on the replacement. Every surveyed
  harness accepts this race; it is rare enough for reasonable rather than ideal behaviour.
- **No diff or abbreviated snapshot mode.** No surveyed harness produces one; the proven lever for
  post-action token cost is returning less, which the existing `query` filter already provides.
- **No refs for elements added since the last snapshot.** They are unaddressable until the next
  snapshot, the same contract as today.

## Work plan

1. **browser-server: in-page ref reuse and pre-action eligibility check.** Outcome: the snapshot
   command keeps refs on unchanged nodes and numbers new ones from a per-document counter; click,
   type_text and select return a distinct error for a ref whose node would no longer be listed by a
   snapshot, using the walker's predicate. Verified by the service's own tests: two snapshots of an
   unchanged page yield identical refs, a node inserted before an existing one does not renumber it,
   a relabelled node gets a new ref, navigation restarts numbering, and an action on a removed,
   hidden or relabelled ref fails fast with the specific error.
2. **Family Assistant: delete the ref cache; mirror the walker; serialise browser actions; attach
   snapshots to misses.** Outcome: `ref_cache` and `clear_refs` are gone from the backend protocol,
   the local session and the computer-use tools; the local walker matches browser-server's and the
   local backend performs the same pre-action check; browser actions for one conversation run one at
   a time and a queued action is refused once an earlier one has replaced the document; a miss
   returns error plus snapshot; tool descriptions and the `/browse` prompt state the contract; the
   browser automation user guide is updated. Verified by the existing functional tests rewritten for
   the new contract, including click-after-`browser_exec` succeeding, click-after-removal returning
   the error with a snapshot, and two ref actions issued from one snapshot both landing on their own
   nodes when run as a batch, and a batched action after a navigating sibling being refused with the
   new document's snapshot rather than acted on.

Milestone 1 merges before milestone 2 starts, because the remote backend's behaviour is what the
functional tests in milestone 2 assert against.
