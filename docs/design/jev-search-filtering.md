# Jev as a search filter

TypeSafe's Jev answers yes/no questions with calibrated probabilities in a few hundred milliseconds
(see [history-compaction.md](history-compaction.md) for the client and the vendor decision). This
document applies it to the places that today decide relevance with a fixed similarity cutoff:
calendar duplicate detection, calendar search and document search.

## Approach

Jev filters; it does not generate. Each candidate a surface has already retrieved gets its own
yes/no question against a shared state, and the candidates at or above a per-surface threshold
survive, most probable first. Because the probabilities are calibrated, the threshold is both the
filter and the cut point, and sorting by probability is the rerank. No separate cut-point model is
needed.

- **The surface's own retrieval stays.** Jev only sees the closest candidates by the surface's
  existing score, so a broad query costs a bounded number of requests, batched 30 candidates per
  request and sent concurrently.
- **Dates and windows stay in code.** Jev is weak at date comparisons, so time windows are applied
  before it runs and the duplicate check passes it the gap in minutes rather than asking it to work
  that out.
- **Shadow first, fail open.** Each surface has `off | shadow | active`. Shadow asks and records,
  and the surface keeps its own scoring; a timeout or error always falls back to it.
- **Bounded injection.** Answers are schema-constrained, so injected text in one candidate can at
  worst change which already-retrieved candidates surface, or whether a duplicate warning fires,
  which the model can override with `bypass_duplicate_check`. It reaches no tools and produces no
  text.

## Surfaces

| Surface                           | Question                                          | Default threshold |
| --------------------------------- | ------------------------------------------------- | ----------------- |
| Calendar duplicate check          | Is the new event the same real-world occurrence?  | 0.6               |
| `search_calendar_events` (text)   | Is this event what the search is looking for?     | 0.5               |
| `search_documents`                | Does this result help answer the query?           | 0.5               |

The duplicate threshold is above an even chance because a match blocks the write until the model
retries; a miss only costs a duplicate the user can delete.

## Evaluation

Each run logs one line with Jev's probabilities, what the surface's own scoring kept and what Jev
would have kept, and counts `family_assistant_jev_filter_decisions` by how the two sets compare.
Implicit labels exist for judging the difference: a bypassed duplicate warning is a false positive,
and a document the model goes on to fetch in full was relevant.

## Deliberate simplifications

- **Shadow evidence lives in logs and metrics, not a table.** The history-compaction shadow data has
  a table because it is read per event; here the question is aggregate (how often would Jev change
  the result, and in which direction), which the counter answers. A table can follow if the logs
  prove too coarse.
- **Calendar candidates carry title, time and calendar name only.** Search results do not carry
  location or description today, and adding them across CalDAV, iCal and Google is separate work.
- **Candidates outside the top N by similarity are never shown to Jev** (30 for a duplicate check,
  90 for a search). They are the least similar by title, and a search that broad is better narrowed
  by date.
