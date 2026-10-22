# Live model experiments

**Status:** implemented, 2026-10-22. Builds on per-turn feedback (thumbs up/down in the web chat,
stored in `turn_feedback`).

## Goal

Answer questions like "is Gemini 4 Argon as good as Opus 5.5 for everyday chat?" from real use
instead of an offline eval set. Some share of ordinary turns run on arm A and the rest on arm B,
assigned at random and without the person knowing which. Each turn records which arm served it, and
the ratings people give are compared per arm.

## What already exists

- **Per-turn feedback.** `turn_feedback` holds one rating (up/down, optional comment) per person per
  turn, keyed by `turn_id`.
- **Per-turn model record.** Every assistant row's `message_history.reasoning_info` already carries
  `model_tier`, `model_tier_source` and the concrete `model`, written by `stamp_model_selection`.
  Joining feedback to it by `turn_id` gives "rating by tier/model" today, with no experiment at all.
- **One chokepoint for a turn's tier.** `ProcessingService._bind_run` decides a turn's tier once, in
  order: explicit choice, then the conversation's sticky choice, then Auto. Everything a turn runs
  on passes through `resolve_model_selection`'s admission gate.
- **Exact-model tiers.** Presets such as `claude_opus_5_5` and `gemini_4_argon` already exist as
  tiers, so an arm is just a tier id.

So the experiment mechanism is small: one more step in `_bind_run`, one more selection source, and a
config block.

## Design

### Configuration

A top-level `experiments` list in config (operator-owned, like `model_tiers`):

```yaml
experiments:
  - id: argon-vs-opus-2026-10
    profiles: [default_assistant]
    arms:               # tier id -> weight
      claude_opus_5_5: 1
      gemini_4_argon: 1
    enabled: true
  - id: frontier-fable-vs-astra-2026-10
    profiles: [default_assistant]
    tiers: [frontier]
    arms:
      claude_fable_5_1: 1
      gemini_astra: 1
```

Startup validation: every arm must be a tier each listed profile can run on (`runnable`), and no
turn can be covered by two enabled experiments. Turning an experiment off is a config change; turns
already recorded keep their arm.

### Which turns are enrolled

An experiment covers a set of turns, and the rule is the same for both kinds: a covered turn runs on
an arm instead of the tier it would otherwise have run on.

- **Without `tiers`**, it covers turns **nothing else has chosen a tier for**: no explicit choice on
  the turn, and no sticky choice carried from earlier in the conversation. A person who picks a tier
  is left alone. This is the everyday-chat comparison.
- **With `tiers`**, it covers every turn on those tiers, whoever chose them. This is how to compare
  candidates for a tier people pick deliberately, such as Frontier (Fable against Astra): nobody
  lands on Frontier by default, so an experiment on unchosen turns would never see it. The person's
  choice is kept in `requested`, so it stays sticky and the tier picker still shows Frontier; the
  arm is just which model serves that choice in this conversation.

An experiment naming a turn's tier wins over one on unchosen turns. A person who chose Auto asked
for routing, so their turns are not enrolled.

Wakes, reminders and delegation results in an assigned conversation run on that conversation's arm,
for the same reason sticky tiers carry to wakes: they extend the same history, and switching models
throws away the prompt cache. Delegations into another profile are not enrolled in the first version
(the target profile decides its own tier as today).

An enrolled turn is not Auto-routed, so Auto shadow decisions are not collected on enrolled turns
while an experiment runs. That is acceptable: both are evaluation tools and they should not stack.

### Assignment: per conversation, deterministic

The arm is `weighted_choice(hash(experiment_id + ":" + unit))`, where the unit is the conversation.
That makes it:

- **Sticky without storage.** Every turn in a conversation lands on the same arm, so the prompt
  cache survives and a conversation is not judged half on one model. Re-deriving it each turn means
  no assignment table to drift from what the turns actually ran on, the same reasoning sticky tiers
  use.
- **Balanced across conversations**, which is the unit people experience.

**Home is the exception**, as it is for sticky tiers: it never ends, so a per-conversation hash
would put a person's Home on one arm for the whole experiment. Home's unit is
`(home conversation id, local date)`: one arm per day, re-drawn daily. That keeps a day's cache and
still gives both arms Home traffic.

### Recording

`stamp_model_selection` gets a new source, `"experiment"`, plus `experiment_id` and `experiment_arm`
written into `reasoning_info` beside `model_tier`. `model_tier_requested` holds the tier a person
chose, where one did. `conversation_model_selection` carries forward a person's choice, read from
`model_tier` on a `user` row or from `model_tier_requested` on an `experiment` row, so an assignment
can never turn into a sticky tier of its own, while a person's choice survives being served by an
arm. Nothing new is stored; the experiment record *is* the turn record.

### Blindness

The web and iOS tier badge on an experiment turn reads "Experiment" instead of the tier name, so
ratings are not biased by knowing the model. The raw `reasoning_info` still reaches the client (a
curious person can see it in devtools). That is fine for a household: this is about not being
nudged, not about secrecy.

### Analysis

First version is a query, not a page:

```sql
SELECT mh.reasoning_info->>'experiment_arm' AS arm,
       count(DISTINCT mh.turn_id) AS turns,
       count(DISTINCT f.id) FILTER (WHERE f.rating = 'up')   AS up,
       count(DISTINCT f.id) FILTER (WHERE f.rating = 'down') AS down
FROM message_history mh
LEFT JOIN turn_feedback f ON f.turn_id = mh.turn_id
WHERE mh.role IN ('assistant', 'error')
  AND mh.reasoning_info->>'experiment_id' = 'argon-vs-opus-2026-10'
GROUP BY 1;
```

Report the rating rate as well as the up share: an arm that gets rated less often may simply be less
memorable either way. Also worth reading: down-rated comments per arm, cost and latency per arm
(already in `reasoning_info`), and how often people manually switch tier away in an assigned
conversation (a strong implicit "this isn't working" signal).

A small read-only experiments page (counts, up share with a confidence interval, recent thumbs-down
comments) is a sensible follow-up once there is data to look at.

### How much data this needs

Thumbs are sparse. To tell an 80% from a 90% up share with reasonable confidence takes roughly 200
rated turns per arm. At household volume that is weeks, not days. Two things help if it is too slow:

1. **Implicit signals** (manual tier switches, the person re-asking the same question, "no, that's
   wrong" follow-ups) recorded per turn.
2. **An LLM judge** over a sample of enrolled turns, scored blind against the transcript, used
   alongside the thumbs rather than instead of them.

Both are follow-ups; neither is needed to start collecting.

## Work plan

1. **Feedback** (done): thumbs up/down per turn, persisted, with optional comment.
2. **Experiment assignment and recording** (done): config block with startup validation, the
   `"experiment"` source, assignment in `_bind_run`, stamping. Verified by web API tests that an
   unselected turn lands on the hashed arm, an explicit or sticky choice is left alone, Home
   re-draws by day, and the assignment never becomes sticky.
3. **Blind badges** on web and iOS (done). Verified by a frontend test.
4. **Analysis**: the query above, also in docs/operations/CONFIGURATION_REFERENCE.md; a read-only
   page later if wanted.

## Deliberate simplifications

- Delegated runs are not enrolled.
- Auto shadow is not collected on enrolled turns.
- Blindness is presentation only, and stays on after an experiment ends; the record keeps the arm.
- Assignment is not stored, so changing an experiment's arms or weights mid-run reshuffles future
  turns. The fix is to start a new experiment id rather than edit a running one.
