# Workhorse Eval Set: Private Evaluation of Everyday Assistant Tasks

## Status

Proposed. This is a plan for approval; nothing here is implemented.

## Goal

Build a **private, household-derived evaluation set** of the tasks the assistant actually does — the
"workhorse" workload on `default_assistant` at the `standard` tier, and the harder tail that reached
`complex_tasks` or the `deep` tier — and a harness that runs the **real agent loop** against those
tasks in a sandbox with representative state and tools, so that a model, tier, prompt or tool change
can be scored on the work it will really be asked to do.

This is an **integration-test-style eval**. The system under test is the whole application as
deployed — prompts, context providers, tool descriptions and policy, the tool-call reviewer, model
routing, the tool implementations — with the model as one parameter. A failure is as likely to be
the harness's as the model's, and the harness is built so the two are told apart (see
"Integration-test framing" below).

Questions the set should answer:

- Does a cheaper or newer model handle the standard-tier workload as well as the current one?
- Which task categories does `standard` fail on, and does `deep` fix them (the evidence Auto
  routing's shadow mode was designed to need — see
  [model-tiers.md](model-tiers.md#observability-and-evaluation))?
- Did a prompt, tool-description, tool-policy or tool-implementation change regress ordinary tasks?
- Where does the application itself get in the model's way: a tool whose description misleads, a
  reviewer that blocks ordinary work, an iteration ceiling hit on a routine task?

The guiding metric, inherited from the tiers design, is *successful useful work per dollar at
acceptable latency*.

### Configurations under test

A candidate is a **model at an effort level**, not just a model: effort is the cheaper dial to turn
before switching models, and the eval should say when it is enough. Each configuration is an
eval-only tier (see "Boot") with a **single-entry chain and no fallback**, so a trial's result is
that configuration's or a visible failure, never a stand-in's answer under its name. The production
recipes, with their routing and fallbacks, are assessed separately afterwards with the winners in
place.

First round, six models in eight configurations:

| Configuration     | Identifier          | Effort | What it answers                                                |
| ----------------- | ------------------- | ------ | -------------------------------------------------------------- |
| Flash, as shipped | `gemini-3.8-flash`  | medium | the Standard baseline                                          |
| Flash, low        | `gemini-3.8-flash`  | low    | whether routine requests finish faster with less thinking      |
| Opus, as shipped  | `claude-opus-5-5`   | high   | the Deep baseline                                              |
| Opus, medium      | `claude-opus-5-5`   | medium | whether lowering effort beats switching models on cost/latency |
| Gemini Argon      | `gemini-4-argon`    | medium | the strongest challenger for long tool sequences               |
| GPT-6.1 Sol       | `gpt-6.1-sol`       | medium | whether the browser tier's model earns a broader role          |
| Claude Sonnet     | `claude-sonnet-5-5` | medium | whether a cheaper Claude keeps enough of Opus's judgement      |
| GPT Luna          | `gpt-6-luna`        | medium | the cheap OpenAI model already on the reminder path            |

Second round, on the cases the first round failed or disagreed on: Argon at high and the Max tier's
Fable at xhigh, to test whether more spend actually rescues failures.

Effort is per chain entry (`llm_parameters`) for the Anthropic and OpenAI adapters today; the Google
adapter sends no thinking level yet, so **Flash low depends on adding that** (in progress
separately). Whatever was sent, the harness records the **effective request parameters** beside
every trial — a configuration that silently ran at the default effort would otherwise look like a
result.

The managed Antigravity coding agent (`coder`) is a different system — server-side execution, its
own effort controls — and is not in this set.

## What exists and is reused

The tool-call review eval ([tool-call-review-eval.md](tool-call-review-eval.md)) already solved the
privacy and process problems; this design borrows its conventions wholesale rather than inventing
parallel ones.

| Piece                                                | Reused for                                                             |
| ---------------------------------------------------- | ---------------------------------------------------------------------- |
| `.review-eval-local/` + `eval/private_paths.py`      | The one gitignored tree all household-derived material is confined to  |
| `scripts/extract_review_history.py`                  | The read-only history access pattern (`mode=ro`, no `init_db`, no WAL) |
| `scripts/dump_tool_registry.py`                      | Snapshot of the deployment's tool surface including MCP tools          |
| `eval/tool_call_review/scrub.py`                     | Deterministic pseudonymisation when a case has to be quoted            |
| Held-out discipline, per-slice reporting, run stamps | Same rules, same report shape                                          |
| `docs/development/eval-results/`                     | Sanitised aggregate results are published there, nothing else          |
| `tests/functional/tools/taint_enforce_harness.py`    | Booting the whole app on the shipped `defaults.yaml` with overrides    |
| `tests/integration/llm/test_shipped_coder_profile…`  | Booting with **real** LLM clients built from the shipped tiers         |
| `POST /api/v1/chat/turns` with `model_tier`          | Driving a turn exactly as a client would, on a chosen tier             |
| `message_history.reasoning_info` tier/routing stamps | Reading back resolved tier, `would_choose`, usage and cost per reply   |
| Radicale fixture, `FakeCameraBackend`, `MockScraper` | Sandbox-side real-ish services                                         |

What does **not** exist: any harness that runs `ProcessingService` with a real model against seeded
state and scores the outcome; a household fixture (notes, contacts, calendar, tasks); fakes for Home
Assistant, weather and MCP servers that the `Assistant` can be configured to use; a clock seam on
`Assistant`.

## Core concepts

- **Task** — one historical turn (or short run of turns) that a household member asked
  `default_assistant` to do: the trigger message, the profile and tier it ran on, the tools it
  called and what they returned, the reply, and the outcome signals around it.
- **Case** — a *journey*, not a recording (the review eval's rule): a user prompt, the household
  state it was asked against, the moment it was asked, and what a good outcome looks like. A case is
  evaluated against the system as it stands today.
- **Household fixture** — a snapshot of the state the assistant reads, materialised into a fresh
  sandbox per case.
- **Sandbox** — a full `Assistant` on the shipped config with real inference for the profile under
  test, in-app state that is real and inspectable, and external integrations replaced by recorded or
  fake backends that **capture** writes instead of performing them.
- **Grade** — deterministic checks on the sandbox's end state plus an LLM judge with the historical
  production reply as a reference, with seeds to measure variance.

## Part 1 — Exporting chat history and extracting tasks

### Access

Production runs PostgreSQL. Take a **one-off dump** into a local database kept under
`.review-eval-local/workhorse/` rather than running extraction against production repeatedly: the
household fixture (Part 2) needs several tables beyond `message_history`, and a local copy makes
every later step reproducible and safe to iterate on. The dump is read with the same read-only rules
the extraction script already enforces. Exclude credential tables outright; keep everything else,
**including the document embeddings**, because retrieval in the sandbox has to run against the
production index (see "Tool surface"). The snapshot therefore needs a Postgres with pgvector.

Two inputs the database does not hold come with it: the **calendar**, which lives on the CalDAV
server and is exported as iCalendar; and the **attachment payloads** the turns reference, copied
from the deployment's attachment storage into the private tree so an image or PDF turn replays with
its bytes. An attachment that can no longer be retrieved excludes its turn from sampling, and the
report states how many were excluded.

### Workload profile

The first snapshot (2026-10-20, profiled in aggregate under `.review-eval-local/workhorse/`)
reshaped the plan, so the shape is recorded here. Of roughly 3,900 user-authored assistant turns in
the last six months, about three quarters are **three templated automation prompts** (an e-ink
display brief and two smaller ones) running a couple of times a day, each at around 150k prompt
tokens per turn. The **human-authored** workload is about a thousand assistant turns and five
hundred complex-tasks turns in six months, split evenly between Telegram and web; a quarter of
assistant turns and two fifths of complex-tasks turns delegate to another profile, most often the
browser profile; a tenth use no tool at all, two fifths use one to three; the long tail of sixteen
or more tool calls is a tenth of assistant turns and a fifth of complex ones. Attachments appear on
one turn in twenty. Shadow routing would send about one assistant turn in eight to `deep`, and those
turns are measurably longer. Correction-shaped follow-ups are rare (under one percent), so the
outcome signal is a weak sampling aid, not a label.

Consequences: the templated automations are **their own slice** — a handful of cases, each run many
times, where a cheaper configuration is worth the most money; the human set is sampled from a
smaller population than first assumed, so sixty cases is a meaningful fraction of it; delegation is
part of the ordinary workload and the sandbox has to run it (see "Tool surface"); and the
in-app/external split of tool calls is roughly ninety to ten, so the backing work concentrates on a
short list: web search, Home Assistant, the task tracker, Google mail and drive, transport, and
shopping lookups.

### Export: turn records

A new script, a sibling of `extract_review_history.py`, walks `message_history` by `turn_id` for
`default_assistant` and `complex_tasks` — including turns `default_assistant` delegated to
`complex_tasks`, and turns a user escalated with `/deep` — and emits one **turn record** per turn
into the private tree. The hard tail is in scope on purpose: those turns are where a standard-tier
candidate is most likely to differ, and where a harness bug (an iteration ceiling, a tool that
returns too much) is most likely to show. Each record holds:

- the trigger: user message text, attachments metadata, interface, user id, timestamp;
- the run envelope from `reasoning_info`: resolved tier, requested tier and source, routing outcome
  and `would_choose`, serving model, token usage, iteration count, wall-clock;
- the trace: each tool call (name, arguments) and its result row, confirmation requests, error rows;
- the reply text;
- **outcome signals** computed from the surrounding rows: whether the user replied within a short
  window and whether that reply reads as a correction or retry ("no, I meant…", the same request
  reissued, a `/deep` escalation), whether the turn ended in an error row, whether a tool call was
  denied or needed confirmation.

Unlike the review-eval templates, these records hold raw household text. That is deliberate and
matches the review eval's decision for its run records: evaluation needs byte fidelity, the private
tree exists to hold exactly this, and nothing in it is ever committed. The script refuses any output
path outside the tree.

### Stratify and sample

A turn record is tagged along a few axes, then the set is sampled across strata so that it
represents the workload rather than its most common shape:

- **tool signature** — the set of tool families called (none; notes; calendar read; calendar write;
  tasks; search/retrieval; smart home; messaging; web; MCP-backed);
- **intent category** — a closed vocabulary (lookup, create, update, schedule, summarise, remind,
  multi-step) filled by the same cheap-classifier pattern the review-eval history pipeline uses; the
  classifier sees the record but its output is only ever a label;
- **difficulty proxies** — iteration count, tool-call count, `would_choose`, and the outcome
  signals; failures and escalations are over-sampled on purpose, because a set made only of turns
  that went fine cannot discriminate between models.

Curation then checks the sample against a short list of **coverage areas**, each needing both
ordinary successes and known failures, and each asking one thing of the system:

| Area                          | Cases to include                                                                           | Question                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Retrieval and grounding       | facts spread across notes, calendar, email, documents; stale records; near-duplicate names | finds the complete, current answer and cites the real source          |
| Cross-tool completion         | dependent actions, pagination, related-record updates, partial failures                    | reaches the intended end state with no omissions or unrelated changes |
| Analysis and judgement        | conflicting evidence, competing constraints, comparisons needing a recommendation          | resolves what matters and separates fact from assumption              |
| Memory and continuity         | follow-ups, corrected facts, changed preferences, resumed work, compacted history          | applies corrections without resurrecting stale state                  |
| Delegation and tier selection | simple requests that should stay local; hard ones that need another profile                | picks a useful profile or tier and carries the task through           |
| Easy with room to overwork    | one-record lookups, a simple reminder, a question with one authoritative answer            | stops when the task is done instead of buying quality with checking   |
| Multimodal                    | a handful of photo, screenshot and PDF turns, if the traffic warrants it                   | the whole path extracts the right information                         |

Two areas are deliberately thin here. Authority and trust — instructions in retrieved content,
ambiguous authorisation — is the tool-call review eval's job; this set keeps only a few **benign
requests that resemble risky ones**, to measure the friction the reviewer adds to legitimate work.
Engineering investigation (`engineer` profile) needs a different sandbox — a source tree,
diagnostics endpoints, logs — and is a later set built on the same harness.

The output is a ranked candidate list with frequency per stratum, for the maintainer to skim. Target
for the first version: about 60 human-authored cases, with every stratum that has non-trivial
frequency represented, roughly a third drawn from turns with a negative outcome signal, several
short multi-turn sequences, and a separate small slice of the templated automation turns.

Because the sample is deliberately unrepresentative — failures over-sampled, strata balanced — the
extraction **persists each stratum's population frequency** with the case set. Aggregate success,
cost and latency are reported **weighted back to the workload** so they estimate the everyday
workload the goal names, while per-stratum results stay unweighted for diagnosis. A gate decision
reads the weighted figure.

### Curate into cases

A case file (YAML, private tree) holds:

- the prompt and any prelude turns needed for context (a follow-up like "move it to Thursday" is
  meaningless without the turn before it);
- the asked-at timestamp and the user;
- a reference to the household fixture revision and the **recorded tool results** for the turn;
- expectations: deterministic assertions on end state (a calendar event exists whose summary
  matches; a note was updated; **no** message was sent) and a short rubric for the judge, plus the
  historical reply as a reference answer;
- lineage: source turn id, strata labels, dev/gate split.

Curation is manual on top of the sampled list. Most of the work is writing expectations; the
extraction gives the maintainer the trace and reply so they can be derived in a minute each.

## Part 2 — The sandbox

### Boot

Boot a real `Assistant` on `defaults.yaml` with the `taint_enforce_harness` override pattern:
sandbox database, storage paths, no Telegram, deterministic embedder, and **no
`llm_client_overrides` for the profile under test**, so it runs the real client built from the tier
being evaluated. The model router, summariser and tool-call reviewer run their configured models
too: they are part of the system under test, and a reviewer that asks for confirmation is a cost the
eval should see. Provider keys come from the environment as for the LLM integration tests.

The candidate model is expressed as an **eval-only tier**: the harness's config overlay adds one
tier per candidate (`eval_claude_sonnet`, say) to `model_tiers` and widens the profile's
`allowed_model_tiers` to include it, then names that tier on the request. `defaults.yaml` is not
edited, the profile's own default tier is untouched, and a candidate without a configured adapter
fails at boot rather than silently running something else — the same rule the tiers design applies
to presets.

### Integration-test framing

The harness is a **pytest suite**, not a standalone script: one parametrised test per case and
candidate tier, living beside the LLM integration tests and carrying a marker that `pytest.ini`
excludes by default, so it runs only when asked and never in ordinary CI. Three things follow.

- **It reuses the test fixtures.** The database engine fixtures, the Radicale server, the server
  socket and wait helpers, the Gemini and VCR replay plumbing, and xdist parallelism are all already
  there, and every eval run exercises them the way the functional tests do.
- **A failure says whose it is.** Every trial ends in one of a closed set of outcomes before any
  grading happens: `completed`, `tool_error` (a tool raised), `unbacked_tool` (the sandbox had no
  backing for a tool the model called), `reviewer_blocked`, `confirmation_declined`,
  `iteration_limit`, `timeout`, `provider_error`. Only `completed` trials are graded for quality.
  The rest are reported as harness or application findings, broken out per tool and per candidate,
  so a tool that errors under every model is filed as a bug in the tool, and one that errors under
  one model as a finding about how that model calls it. A run whose non-`completed` rate exceeds a
  small threshold is a harness failure first and a model result second.
- **The harness has its own regression tests.** A **self-test slice** runs the whole pipeline —
  boot, fixture materialisation, re-dating, tool backing, turn driving, deterministic grading — with
  the rule-based mock LLM scripted to call tools, on a small committed synthetic household rather
  than the private fixture. It runs in ordinary CI, so the harness cannot rot between eval runs, and
  a change to a tool, the chat API or the reviewer that breaks the harness fails a test rather than
  a paid run.

Each case gets a **fresh sandbox**: the database is cloned from a prepared fixture template (the
SQLite schema-template trick, applied to a seeded database), so cases are independent and can run in
parallel.

### Household fixture

The fixture is produced once from the snapshot and revisioned: notes, memory notes, contacts and
known users, automations, the document index with its embeddings, emails, and the calendar. It is
the household's real data, kept private, because representative results need representative context
— the assistant's behaviour on "add it to the shopping list" depends on there being a shopping list
note in the house style.

- **In-app state** is loaded into the sandbox database (Postgres with pgvector, so the production
  index is queryable as-is).
- **Calendar** is rebuilt from recorded calendar results, like mail: the deployment's calendars from
  `list_calendars`, and every event a turn saw, created or deleted, with its observations over time.
  The writable CalDAV calendar and the read-only iCal feeds are served by one Radicale process; the
  Google calendars by the Google fake. Calendar ids and names are the deployment's.
- **Time.** Each trial runs with the **process clock frozen at the case's asked-at moment** using
  `time-machine`, which patches the interpreter's clock sources so the roughly two hundred direct
  wall-clock reads under `Assistant` all agree, without a clock seam. The fixture is not re-dated:
  state and clock are both historical, so "tomorrow" and "Friday" resolve against the same day the
  prompt was written on, and past calendar events are found where they were. Database-side `now()`
  defaults are the one clock this does not reach; they stamp bookkeeping columns, not anything a
  case asserts on.
- **State is rewound to the case's moment.** The snapshot holds the household after every case in
  it, so replayed as is a case sees its own future: notes its original turn went on to write, later
  documents, the rest of the conversation. Each trial runs on a fresh copy rewound to the moment the
  case was asked. Rows created after it are removed; notes, which keep no history, are rebuilt from
  the recorded reads and writes of them, which reproduce later observations almost exactly; calendar
  events and mail are filtered to what existed then. The prompt is replayed in its original
  conversation, so the rewound history is the context it had. What the record cannot rewind is
  reported per trial rather than guessed, and where a case needs state the record lacks, curation
  adds it as a declared per-case delta.

### Tool surface

The registry snapshot from `dump_tool_registry.py` is the contract: the sandbox must present the
same tool names and schemas the deployment did, or the case is not measuring the same task. The rule
for backing a tool is that **every call either gets a real or deterministic answer, or the trial is
unscorable** — never a plausible stand-in, because a stand-in handed to one candidate and not
another is exactly the non-uniform bias a comparison cannot carry. Four classes, by what backs them:

1. **In-app tools** — notes, memory, calendar via Radicale, scheduling, scripts, delegation, and
   **search over the production index with the production embedding provider**, so query vectors
   match the stored rows — run for real against the sandbox. Their effects are the end state the
   grader inspects. The deterministic test embedder is not used here: it cannot query the imported
   index, and re-embedding with it would change what retrieval returns.
2. **Live read-only services** — web search, weather, maps, transport, shopping lookups — call the
   real service. Their answers drift over time, which is accepted for reads that nothing in the
   household owns: the drift is the same for every candidate in a run, and a replay would reject
   most of the varied queries a search tool receives.
3. **Household-owned external state** — Home Assistant, the task tracker, Google mail and drive — is
   served from **what the household's turns actually saw**, so two candidates phrasing a lookup
   differently both get a correct answer. The snapshot holds no mailbox and no house, but it holds
   every result those tools returned, and pooled across the history that is a small real world: each
   item is some case's needle and every other case's distractor. A case sees the part of it that
   existed when it was asked. No bulk export of the mailbox or the house is taken; one is worth
   adding only if this proves too easy, for instance if every candidate aces the mail cases.
   - **Google mail and drive** are a fake behind the connected-account transport, the one seam every
     Gmail, Drive and Google Calendar request already passes through. Everything above it runs as in
     production: the integration enabled by configuration, the user's connection stored encrypted, a
     token refreshed before every request, the tools' scope checks and parsing.
   - **Home Assistant** is a real `hass` process, as the integration tests already run, configured
     with the entities and states the recorded results show, so templates, service calls and state
     changes behave as they would at home. Recorder history starts empty.
   - What the record cannot supply stays absent rather than invented. A message only ever seen in a
     listing is served with its snippet as its body and the trial is marked partial; content that
     was never recorded at all — attachment bytes, an unread Drive file — is unbacked. A call
     nothing can answer ends the trial as `unbacked_call`, which is excluded from quality metrics
     and reported as a harness gap.
4. **External writes** — messaging, email, Home Assistant service calls, anything that leaves the
   sandbox — go to **capture backends** that record the call and return a success-shaped result.
   Captured calls are part of the end state and are what "no message was sent" assertions check.

Delegation is part of the ordinary workload, so a **delegated profile runs for real inside the
sandbox** on its configured tier, under the same backing rules. The sandbox is **sealed**: profiles
whose work happens outside it — remote agents, the managed coder, computer use, the browser
profiles, deep research — are removed, and every remaining profile carries an operator-layer rule
denying browser, worker, GitHub, authenticated-site and shopping-cart tools, which no profile grant
outranks. A case that depends on one of them is scored as a harness gap.

Services the household owns run **for real, locally**, from the deployment's own source and settings
rather than as fakes: the task tracker (Tuit, seeded through its own API from recorded results, its
clock set to the case's moment), the code-execution server (confined with a `sandbox-exec` profile
in place of its container), and Home Assistant with `ha-mcp` in front of it, built from recorded
entity states. The sandbox starts from the deployment's own `config.yaml` and keeps only the MCP
servers it runs locally, pointed at them.

This is the one place the sandbox needs new seams. Concretely: a generic fake MCP server process
that serves a registry snapshot with seeded-fake, replay or capture semantics per tool covers every
MCP-backed tool in one mechanism; the `Assistant` takes a connected-account transport for Google,
and needs config-selectable backends for the weather service and the outbound messaging paths. Home
Assistant needs no seam: its plugin instance is configuration, pointed at the sandbox `hass`. Prefer
one mechanism keyed by tool name over per-integration fakes, so a tool added later either has a
backing or fails loudly as unbacked, never silently succeeds.

### Measuring the simulation

Coverage is measured, not assumed, two ways. **Traffic replay** re-executes a sample of every tool's
recorded calls through the sandbox's tool API under the recorded profile and classifies each result
against the recording (similar, different, missing tool, missing configuration, new error); weighted
by production call volume this is the coverage figure, and it is re-run whenever backing changes.
**Probing** has agents act as an assistant against the sandbox in one area each, comparing what they
get with the recorded traffic, to find the walls replay cannot: semantics a fake gets consistently
wrong, results that leak the sandbox, state from the future. For a fake that answers queries, the
recorded searches are its conformance suite: each is replayed with its own evidence withheld, and
recall and precision against the real results are reported.

The first measurement found about half of production's tool-call volume backed. In order of the
workload share they gate, the remaining gaps are: Home Assistant (native and MCP, from recorded
entity listings and the snapshot's state-change events, on a real `hass`); web search and the other
read-only lookups (a replay cache keyed on normalised arguments, returning an explicit miss); the
code-execution server; the task tracker (a stateful fake from its recorded results); attachment
payloads, which the snapshot does not hold; and delegations that leave the sandbox (the browser and
deep research profiles), which must be backed or blocked, because a live answer is neither
reproducible nor the household's.

### Driving a turn

The runner POSTs to `/api/v1/chat/turns` with the case's prompt, `profile_id`, the configured
`model_tier`, and `interface_type: "api"` as the acting user, follows the stream to completion,
answers confirmation prompts with a per-case policy (default: approve, and count it), and then
collects: reply text, the tool-call sequence, confirmations, errors, iteration count, the
`reasoning_info` envelope (tier, serving model, usage), latency, and the sandbox state diff.

The sandbox runs with real web authentication, and the runner calls as the case's household user
with an API token. Connected-account integrations refuse the shared development identity by design,
and the user-scoped state a case reads (notes visibility, reminders, the Google connection) belongs
to the real user, so running as anyone else would test a different assistant.

## Part 3 — Grading and reporting

Three layers, cheapest first:

1. **State checks** by a grading agent that holds the household's own read tools, called through the
   sandbox's tool API as the household user after the turn, with the clock still at the case's
   moment. It inspects the calendar, notes, mail, the house and the task list the way the household
   would, rather than reading the action log, and records each check with its evidence, any unwanted
   effects (the wrong item, duplicates, more than asked), and whether the reply was true. Write
   tools are never offered to it. Where a case states expectations, they are given to it as checks
   to make.
2. **LLM judge** for the reply: a rubric score (did it do the thing, is the information correct
   against the fixture, is it concise) and a **pairwise preference** against the historical
   production reply, with position swapped. The judge runs on a strong model from a different
   provider than the one under test. Judge prompts are versioned and their hash is stamped on the
   run.
3. **Human spot-check** of a fixed sample per run, recorded as agreement with the judge, so judge
   drift is visible.

Every case runs with several seeds (three to start); seed disagreement is reported per case the way
the review eval reports verdict flips. The judge is blinded to which configuration produced a reply,
and presentation quality is scored as its own line so polished writing cannot hide a wrong result.

### Scorecard

Per configuration and per stratum, in this order of importance:

1. **Correct completion** — the primary metric. Required outcomes achieved with the right people,
   records, dates and quantities; no unintended changes; the reply's claims agree with what actually
   happened. Authority violations and wrong side effects are explicit failures, never averaged in.
2. **Spend per successful task** — cost per attempt and cost per success (all spend across every
   attempt, divided by successes), counting the parent turn, every delegation subconversation,
   retries and failed attempts. The application records token usage, not money, so the harness keeps
   the **raw usage per call** and prices it at report time from a versioned price table; results can
   be repriced later without rerunning (Argon's introductory pricing is the near example).
3. **Experienced latency** — time to first useful token or action, time to completion, median and
   tail with sample counts, and model wait versus tool wait, so a fast acknowledgement cannot make a
   slow task look responsive. Also the fraction of tasks completed within a chosen time budget.
4. **Supervision required** — clarifying questions that were unnecessary, confirmations asked,
   corrections the user would have had to make, manual recovery.
5. **Efficiency and stopping** — model rounds, tool calls, repeated searches, retries, delegations;
   whether extra verification changed correctness and whether the model stopped once the task was
   adequately done. This is the layer that explains *why* a configuration costs more.
6. **Answer usefulness** — the judge's rubric: correctness, completeness, evidence, clarity,
   appropriate uncertainty, with presentation separate.

A run report, written to the private tree, carries all of this plus the harness findings (outcome
taxonomy, unbacked-tool fallbacks hit). A **sanitised summary** (counts, rates, strata labels, no
text) is what gets published under `docs/development/eval-results/`, in the existing shape. The
report the maintainer actually wants is the success–cost–latency table per profile and the routing
decisions it supports: which requests Flash handles adequately, where lower effort pays off, where
Argon earns its cost, where Opus still materially improves the outcome.

Held-out discipline follows the review eval: the case set is split into dev and gate slices by
stratum at curation time; prompt and routing tuning uses dev; a model-switch decision reads gate,
and reading it spends it.

## Work plan

Each milestone is independently useful and verifiable.

1. **Snapshot and export.** The table-restricted dump procedure and the turn-record export script,
   with outcome-signal computation. Verified by running it on the snapshot and on a dev SQLite
   database, checking the private-path refusal, and eyeballing record counts per stratum. Outcome: a
   ranked candidate list to curate from.
2. **Case schema and first curated set.** The case model (pydantic, `extra="forbid"`, slug ids,
   private-tree loader) and about 60 hand-curated cases with expectations, roughly a quarter from
   `complex_tasks` and `/deep` turns. Verified by loading all cases and round-tripping them; a case
   with text in an id or an unknown field fails to load.
3. **Sandbox boot and fixture materialisation.** Shipped-config boot with real clients and the
   eval-only tier overlay, a pgvector-backed fixture template carrying the production index, the
   frozen process clock, Radicale seeding from the calendar export, per-case clone, as a pytest
   suite behind an opt-in marker. Verified by the first self-test: one synthetic case with the mock
   LLM scripted to call a calendar tool "tomorrow" relative to a frozen date, and the end state
   showing the event on the right day, running in CI.
4. **Tool backing.** The Google fake and its transport seam, a sandbox `hass` built from recorded
   Home Assistant results, the fake MCP server with seeded-fake, replay and capture semantics for
   the task tracker and other MCP tools, live read-only wiring for search and weather, and the
   config seams for weather and messaging. Verified by a conformance check that every tool in the
   registry snapshot has a backing class for every case, failing the run otherwise, and by self-test
   cases for each class including an unbacked call ending a trial as unscorable.
5. **Runner, outcome classification and deterministic grading.** Drive turns, collect traces,
   classify each trial's outcome, apply the checks, write run records. Verified by running the dev
   slice on the current `standard` tier, inspecting a handful of traces by hand, and fixing every
   harness-attributed outcome before any model is judged on the set.
6. **Judge, seeds and report.** Rubric and pairwise judging, seed flips, per-stratum report,
   sanitised publish. Verified by a judge-vs-human agreement check on the spot-check sample.
7. **First real comparison.** Run the first-round configurations on the gate slice; publish a
   sanitised result. Outcome: the first evidence for the tiers design's open question of whether
   `deep` improves the categories Auto would route there, the first ranking of candidates on the
   real workload, and the list of cases the second round (Argon high, Fable xhigh) should target.

## Deliberate simplifications

- **The process clock is the only clock frozen.** Database-side defaults and external services see
  real time. That reaches bookkeeping columns and the live read-only services, not the state a case
  asserts on; a case that would depend on it is excluded at curation.
- **Live reads drift.** Web search, weather and transport answers change between runs; a comparison
  within one run sees the same drift for every candidate, and runs are not compared on those tools'
  content across time.
- **Rewind is as good as the record.** Notes edited outside a recorded turn, or deleted without one,
  cannot be placed exactly; such notes keep their snapshot content and are reported. Automations and
  event listeners stay disabled rather than rewound, since a frozen clock would otherwise fire them
  mid-trial.
- **Household-owned services see only what was recorded.** Mail bodies, Drive content and calendar
  descriptions exist only where some turn read them; search over the rest falls back on recorded
  search verdicts, measured by replaying every recorded search with its own evidence withheld.
- **Single-turn by default.** Multi-turn journeys are supported through preludes, but the first set
  is dominated by single turns because that is what the workload is and what can be graded cleanly.
- **Raw text lives in the private tree.** No pseudonymisation on the hot path; the existing scrubber
  is used only when quoting a case outside it.
- **Judge preference against history is a signal, not ground truth.** The historical reply came from
  a model that may itself have been wrong; the deterministic checks and the spot-check are what
  anchor it.

## Decisions taken

- **`complex_tasks` is in scope** from the first set, as the hard tail.
- **The harness is as much under test as the model.** Hence the pytest form, the closed outcome
  taxonomy that separates harness and application failures from model failures, and the self-test
  slice in CI.
- **The tool-call reviewer runs its configured model** during eval runs; confirmations are counted
  as friction rather than pinned away.
- **Candidates are model-at-effort configurations with no fallback**, assessed on the real prompts,
  tools and adapters; the production recipes with routing and fallback are assessed after, with the
  winners in place.
- **First round:** Flash medium and low, Opus high and medium, Argon, GPT-6.1 Sol, Sonnet and Luna;
  second round adds Argon high and Fable xhigh on the failures.
- **Engineer and coder are out of this set.** Engineer gets a later set on the same harness;
  Antigravity coding is a different system.

## Open questions

- Whether a `complex_tasks` case runs on that profile (its prompt and iteration ceiling) with the
  candidate as its tier, or on `default_assistant` to ask whether the delegation was needed at all.
  Proposed: both, as two cases sharing one journey, since the second is the question Auto routing
  exists to answer.
