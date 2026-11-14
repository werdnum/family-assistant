# Workhorse eval: turn extraction

**What's here:** how to export historical assistant turns and the stratified candidate list the
workhorse eval is curated from. The design is
[docs/design/workhorse-eval-set.md](../design/workhorse-eval-set.md); this is milestone 1.

## Run it

Restore a production snapshot into a local PostgreSQL (or point at any database you may read), then:

```bash
poe workhorse-extract-turns --database-url "$DATABASE_URL" --since 2026-04-23
```

The command reads the database read-only and never initialises its schema. It writes three files to
`.review-eval-local/workhorse/turns/` and refuses any destination outside the private tree, and any
destination that is not empty:

| File              | Contents                                                                                                                                     |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `records.jsonl`   | One record per turn: prompt, tool calls with results, reply, errors, usage and tier, wall time, and the outcome signals.                     |
| `summary.json`    | Parameters, counts per population and profile, and each stratum's turn count — the frequencies results are weighted back to.                 |
| `candidates.yaml` | The stratified sample to curate, each entry with its stratum, weight, prompt excerpt and signals, and a `decision: null` field for curation. |

All three hold household text. Never commit them or paste them anywhere public.

## How turns are classified

- **Population.** A delegated run's opening request, written by the delegating model, is
  `delegated`. A prompt starting with a templated-automation prefix (the e-ink brief, callback
  triggers, the self-contained page generator) is `templated`; override the list with repeated
  `--templated-prefix`. Everything else is `human`.
- **Stratum.** `population/profile/family`, where family is the tool family the turn called most
  (ignoring tool-discovery bookkeeping), or `none`. Templated turns are one stratum per template.
  Delegated runs are recorded and counted but never sampled: a case replays the turn that delegated,
  and the delegated profile runs for real inside it.
- **Outcome signals** are weak labels for steering the sample, not verdicts. They read only later
  *human* turns of the extracted profiles in the same conversation, so a delegated run or an
  automation landing seconds later does not count: a follow-up within ten minutes, a follow-up that
  opens like a correction, an escalation (the follow-up ran on a different extracted profile, such
  as re-asking on `complex_tasks`), and any error row or traceback.

## How the sample is drawn

Human turns share `--target` picks (default 60) in proportion to the square root of each stratum's
size, with every stratum getting one pick while the target allows. Inside a stratum, picks rotate
across tool-call size buckets so larger strata reach their long turns, and up to `--negative-share`
(default a third) go to turns with a negative signal. Each templated automation contributes
`--templated-per-template` picks (default 2). The order is a hash of `--seed` and the turn id, so
the same snapshot and seed always give the same sample.

Each candidate's `weight` is the number of turns it stands for. Because picks over-represent
negative-signal turns and rare sizes, a pick stands for its cell within the stratum (negative signal
by size bucket) rather than for the whole stratum, and cells no pick reached are spread over the
picked ones, so a stratum's weights still sum to its size. Aggregate results are weighted by it to
read as the workload rather than as the sample.
