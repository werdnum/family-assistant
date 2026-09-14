# Reconciling pollable delegations against late provider outcomes

## Problem

A pollable delegation treats the first terminal thing it sees as the truth. One provider
observation, one local timeout, or one transport failure finalizes the run, and whatever the
provider does afterwards is never looked at again.

That assumption does not hold. An audit of six locally failed `coder` runs found that none of them
still reported the status Family Assistant had recorded: one had since `completed` with substantial
work that was thrown away, one had `completed` with nothing in it at all, and four were back to
`in_progress` having previously been observed `cancelled`.

Three distinct defects follow from the single-observation model:

- **Lost work.** A run finalized `failed` on a transient provider status discards a result the
  provider later produces.
- **Claims we cannot support.** A local timeout reports "the run was cancelled" when all that
  happened is that we asked; the provider may never have cancelled anything.
- **Empty success.** A bare `completed` with no output, no steps and no usage is delivered as a
  successful answer, which reads to the user as the assistant having nothing to say.

The provider-side causes are out of scope. The application has to stay correct against remote state
that is delayed, changing, eventually consistent, or briefly unavailable.

## Approach

Two ideas carry the design.

**Local disposition is not remote status.** A run's `status` records what Family Assistant decided
to do; a separate set of fields records what the provider was last seen to say, and when. Neither is
derived from the other. "We timed out locally" and "the provider reports cancelled" are different
facts that were previously collapsed into one, which is why the run could claim a cancellation
nobody confirmed.

**One classifier, two callers.** Every read of a remote run goes through a single place that turns
it into a bounded `RemoteObservation` carrying a `RemoteDisposition`. Polling and reconciliation are
then the same question asked at different times, and the rules for "this completion is empty" or
"this status is terminal" cannot drift between them. A provider that cannot be observed simply does
not implement the capability, and reconciliation skips it rather than guessing.

Reconciliation is a re-read, never a mutation: it never cancels, deletes, resumes, or re-submits.
Its only power is to notice that a run we gave up on has since produced something and to deliver
that result once.

### What is persisted

The observation is deliberately small and fixed-shape: status, the provider's own timestamps,
whether output is present and how long it is, step count, usage and resolved model, a truncated
error summary, and an event cursor. It never carries the agent's thoughts, its prompt, its command
output, or anything from the sandbox environment — the serializer builds a closed set of keys rather
than copying a provider payload, so a future provider field cannot leak in by default.

Alongside it the run records why *we* failed it (`local_failure_kind`), when cancellation was
requested and when it was actually confirmed remotely, how many reconciliation reads have happened,
and whether reconciliation has settled.

### Recovering a late result

A late success is recovered through the ordinary result path: the same delivery, the same persisted
taint, the same tool-call-review rules. The run's original failure is retained in `error` — the
history is not rewritten, it is annotated — and `late_recovered_at` marks the run as reconciled
rather than as a run that simply succeeded.

Recovery happens exactly once, and that is a property of the transition rather than of the
scheduler: it is a compare-and-set on `status = 'failed' AND late_recovered_at IS NULL`, so
concurrent reconcilers, a re-enqueued sweep and a retried task can all attempt it and exactly one
wins. Stale observations are rejected the same way, by refusing to write an observation stamped
earlier than the one already stored — stamped when the read was *issued*, since that is the only
bound this side can prove on how old the state it describes may be.

Recovering once is not the same as delivering once, and this does not claim the latter: terminal
delivery sends before recording `notified_at`, so a crash in that window re-sends on the next sweep.
That at-least-once window is the existing delegation delivery protocol's, not something
reconciliation introduces, and a late result inherits it like any other terminal result.

### Bounded work

Reconciliation is bounded on three axes: a maximum number of reads per run, a maximum age past which
a run is abandoned, and exponential backoff between reads. The first reads happen soon after the
local failure (that is where the useful late completions were found); later ones are rarer. A run
that reaches any bound is marked reconciled and never looked at again.

The recurring delegation cleanup sweep re-enqueues reconciliation for eligible runs that have no
live task, which is what makes the mechanism idempotent and crash-tolerant without a second
scheduler.

## Deliberate simplifications

- **A run observed `in_progress` after we failed it is not resumed.** We keep reading it until it
  settles or we hit a bound. Re-adopting it as an active run would mean un-terminalizing a run the
  user was already told about, which is a larger change than the value justifies.
- **Reconciliation never mutates remote state.** It does not chase a run to a conclusion by
  cancelling it, which keeps the sweep safe to run against runs whose provider semantics we do not
  fully know.
- **A recovered result does not assert that the failure notice was delivered.** It says the run
  failed earlier and finished after all, which is true of the run whether or not the notice reached
  anyone. Tracking delivery of a superseded notice, so the wording could assert it, would buy
  nothing the reader needs.
- **Empty completion is judged per profile, not per provider.** A profile whose whole purpose is to
  return a result declares that it expects output; a completion with no output and no execution
  evidence is then classified as empty rather than successful. Profiles that do not declare it keep
  the old behaviour.

## Work plan

1. **Observation vocabulary and persistence** — `RemoteObservation` / `RemoteDisposition` /
   `ObservableDelegationService`, the new delegation-run columns and their migration, and the
   repository transitions (stale-guarded observation write, late-completion CAS). Verified by
   storage tests over both backends: stale writes rejected, recovery CAS won exactly once.
2. **Single classifier for Interactions agents** — `observe_async` on the Interactions agent service
   with `poll_async` reimplemented on top of it, including empty-completion classification. Verified
   by service tests asserting poll and observation agree on every status, and that an output-less
   completion is not a success.
3. **Reconciler** — the `delegation_reconcile` task, its enqueue-on-failure hook, the backoff and
   bounds, and the sweep that re-enqueues lost reconciliations. Verified by worker tests covering
   cancelled→completed, timeout→late completion, unconfirmed cancellation, idempotent sweeps and
   exactly-once delivery.
