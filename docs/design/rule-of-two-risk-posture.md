# Rule of Two — Prompt-Injection Risk Posture

**Status:** Living document / risk assessment **Scope:** How well Family Assistant adheres to the
"Rule of Two" agent-security principle, where it deliberately trades purity for utility, and where
it diverges from its own documentation by accident.

> This is a risk-management document, not a compliance checklist. The Rule of Two was never intended
> as an absolute constraint here; it is a lens for reasoning about prompt-injection blast radius.
> The goal of this document is to make the trade-offs explicit — what utility each relaxation buys,
> what risk it incurs, and whether that relaxation is a deliberate choice or an unnoticed gap — so
> we can decide each one on its merits.

## 1. The model in one paragraph

The Rule of Two (from Meta's practical AI-agent-security framing) says an agent session should
satisfy at most **two** of three properties:

- **[A]** processes untrustworthy input,
- **[B]** accesses sensitive systems or private data,
- **[C]** can change state or communicate externally.

A session with all three (**[ABC]**) is the dangerous case: attacker-controlled text ([A]) can steer
a model that can both read your private data ([B]) and send it somewhere or take an action ([C]) — a
complete prompt-injection exfiltration/actuation chain. The whole point is to ensure that any
session which touches untrusted input is missing either the sensitive data or the outbound channel,
so an injection cannot complete the loop.

Family Assistant's baseline posture, stated in `AGENTS.md`, is that it "primarily operates in
**[BC]** mode": authenticated users talk to a fully-capable assistant, and safety rests on **strong
authentication at the interface boundary** keeping [A] out of that [BC] context. That is a coherent
and defensible design. **The risk is therefore entirely about the seams: every place where untrusted
content can leak into a [BC] session turns that session into [ABC].** This document catalogs those
seams.

## 2. Ground truth: what the profiles actually are

`AGENTS.md` describes five conceptual profiles. Mapping them to what is actually shipped in
`defaults.yaml`:

| AGENTS.md concept            | Reality in `defaults.yaml`                                                                                                                    | A/B/C                    | Notes                                                                                                                                                              |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Trusted **[BC]**             | `default_assistant`, `complex_tasks`, `telephone`, `browser_profile`, `automation_creation`, `artist`, `data_visualization`, `camera_analyst` | **[BC]**                 | Full or near-full toolset; the workhorse profiles.                                                                                                                 |
| Untrusted-Readonly **[AB]**  | **No profile with this name.** Closest analog: `email_intake`                                                                                 | **[AB] + human-gated C** | Reads calendar/notes/docs; all writes/sends are `confirm`-gated via durable confirmation, not disabled. A fourth category the docs don't name.                     |
| Untrusted-Sandboxed **[AC]** | **No profile with this name.** Closest analog: `telephone_external`                                                                           | **[AC]**                 | Untrusted external caller; can `send_message_to_user` without confirmation, but [B] is *starved* via `visibility_grants: ["external_facing"]` rather than blocked. |
| Engineer **[B]**             | `engineer`                                                                                                                                    | **[B]**                  | Read-only source/DB/logs; only side-effecting tools (`create_github_issue`, `reconnect_mcp_server`, delegation) are `confirm`-gated. Cleanly matches the doc.      |
| Complex Tasks **[BC]**       | `complex_tasks`                                                                                                                               | **[BC]**                 | Same breadth as `default_assistant`, Opus model.                                                                                                                   |

**Key takeaway:** the two profiles the docs lean on hardest for handling untrusted input —
"Untrusted-Readonly [AB]" and "Untrusted-Sandboxed [AC]" — **do not exist as named, shipped
profiles.** `AGENTS.md` itself hedges this in its "Future Considerations" section, calling explicit
processing profiles something that "will become increasingly important" as the system "evolves to
handle more untrusted inputs." So the conceptual framework is partly aspirational. The real
untrusted input handling today rests on `email_intake` (well-hardened) and a scattering of implicit
choices elsewhere (mostly *not* hardened — see §4).

The policy engine that enforces all this (`src/family_assistant/tools/policy.py`) is
**default-deny**: a tool not matched by any allow rule is denied. Layers are
`defaults (0) < profile (+100) < operator (+1000)`, first match in sorted-priority order wins, and a
`CONFIRM` decision downgrades to `DENY` if there is no live confirmation channel. This is a sound
foundation — the failure modes are about *what rules are written*, not the engine.

## 3. Where the Rule holds — the genuine strengths

It is worth being clear that several parts of this system enforce the Rule of Two well, because they
are the model for fixing the parts that don't:

- **`email_intake` is properly contained.** It denies every destructive/code/browser/delegation/
  worker/automation tool by tag, confirm-gates all writes and sends through durable out-of-band
  confirmation, and cannot `delegate_to_service` at all. Untrusted email content is wrapped in
  `<untrusted_email_evidence>` tags with a regex neutralizer that strips sender attempts to spoof
  the closing tag (`email_intake/actions.py:31-77`). This is real, defense-in-depth [AB] handling.
- **Durable confirmations are strong and replay-safe.** The `confirmation_requests` table stores the
  *exact* tool name and args; on approval the tool is **re-evaluated against live policy** and only
  executes if the reconstructed call matches the stored one byte-for-byte. Policy drift fails
  closed; a past approval cannot authorize a different future call. Attacker-controlled values shown
  in confirmation prompts are code-fenced with dynamic fence-length extension to prevent formatting
  breakout, and oversized delegation requests are *refused* rather than truncated so a human can't
  rubber-stamp a partial preview.
- **The `engineer` profile is a clean [B].** Read-only DB (SELECT-enforced two ways), read-only
  source, redacted config; its only outbound tool (`create_github_issue`) is confirm-gated;
  delegating into or out of it always requires confirmation.
- **Browser/computer-use profiles are kept at [AC], not [ABC].** `browser_profile` /
  `browser_visual_profile` deliberately have no notes/calendar/documents/Home-Assistant access, so
  the inherently-untrusted web content they ingest cannot be combined with private data.
- **Script automations are capability-monotone.** `wake_llm`'s sibling action type, `SCRIPT`,
  threads `processing_profile_id` from creation to execution, so a script authored under a
  restricted profile cannot later run with a more privileged toolset (`automation_provenance.md`,
  verified in code). This is the one place real provenance tracking exists and works.

## 4. Where the Rule breaks — findings, ranked

Findings are labeled **BUG** (code diverges from documented/intended design — should be fixed),
**TRADE-OFF** (a deliberate or defensible relaxation whose risk we should consciously accept or
revisit), or **DEPLOYMENT** (safe-by-config but unsafe-by-default, an operator footgun).

### F0 — `wake_llm` event-listener actions run under the full-trusted `default_assistant` profile — **BUG, highest severity**

**This is a genuine, reachable [ABC] chain and a documentation/implementation mismatch, not a design
choice.** Verified directly in code (`src/family_assistant/actions.py:65-96` and
`src/family_assistant/task_worker.py:477-556`), and independently reached by two separate
investigations.

- `docs/design/automation_provenance.md` and `event-listener-system.md` state that `wake_llm`
  actions "intentionally keep running under the restricted `event_handler` profile." The
  `actions.py` docstring repeats this claim.
- In fact, the `ActionType.WAKE_LLM` branch **never reads `processing_profile_id`** — it builds an
  `LlmCallbackPayload` without it. (The `ActionType.SCRIPT` branch three lines down *does* thread
  it.)
- `handle_llm_callback` only swaps to a non-default profile for the `is_reminder` flag (→
  `reminder`). Every other `llm_callback` — i.e. every event-listener wake-up — runs under the task
  worker's default `processing_service`, which is `default_assistant` (full [BC] toolset, most tools
  `allow` with no confirmation).
- `grep` for `"event_handler"` across the runtime event path returns zero hits: **the restricted
  `event_handler` profile is never actually selected for its stated purpose.** It is effectively
  dead code.

**Attack chain:** an attacker emails the user; the email is indexed and emits a `DOCUMENT_READY`
event whose title/metadata is attacker-controlled; a pre-existing user listener ("notify me when the
newsletter arrives") matches and fires a `wake_llm`; the attacker-influenced trigger text is
injected **with no neutralization** into a `default_assistant` turn that can `search_documents`
(pulling the full attacker email body), `send_message_to_user`, `call_home_assistant_action`,
`mqtt_publish`, `spawn_worker`, write notes, etc. That is [A]+[B]+[C]. Reachability depends on the
operator having a webhook-/indexing-sourced listener with a `wake_llm` action — an explicitly
supported, documented feature, so this is not merely a misconfiguration.

**Fix direction:** route `wake_llm` through the same profile-resolution path already built for
`SCRIPT` actions (thread `processing_profile_id`), or force `event_handler` explicitly. Note F1
before relying on `event_handler`.

### F1 — the `event_handler` profile is not actually read-only — **BUG (latent)**

Even if F0 were fixed by routing to `event_handler`, that profile (`defaults.yaml:746-779`) grants,
unconfirmed: `add_or_update_note` (write, [C]), `send_message_to_user` (external comm, [C]),
`search_documents` (sensitive read, [B]), `mqtt_publish` ([C], can actuate smart-home devices), plus
the Home Assistant MCP server. The docs call it "read-only and non-destructive." As written it is
itself [A]+[B]+[C] when handling event data. Fixing F0 without also tightening `event_handler` would
reduce, not eliminate, the exposure.

### F2 — stored untrusted content is readable, unmarked, by every trusted profile — **TRADE-OFF (needs a decision)**

Emails are indexed into the `documents` store and are **not** excluded from `search_documents` /
`get_full_document_content` (`tools/documents.py:59` excludes only `message_history`). Any [BC]
profile — `default_assistant`, `telephone`, `data_visualization`, `artist`, `engineer`,
`event_handler`, `reminder` — returns attacker-authored email bodies as plain text on a simple
"summarize my email" or "search my documents" request. **The `email_intake` neutralization does not
travel with the content**; it is applied only inside `email_intake`'s own trigger prompt. This is
classic second-order (stored) injection: untrusted text enters a [BC] context through the back door
of retrieval, turning it into [ABC].

The utility this buys is real and central to the product: the assistant *should* be able to answer
questions about your email. The trade-off is that there is no per-document trust label
distinguishing "human-written note" from "attacker-authored email body," so retrieval cannot warn or
restrict based on provenance.

### F3 — the taint taxonomy exists but is inert — **TRADE-OFF / unfinished work**

`ToolTag.OUTPUT_UNTRUSTED` / `OUTPUT_TRUSTED` is correctly applied to ~a dozen tools
(`search_documents`, `get_full_document_content`, `read_text_attachment`,
`ingest_document_from_url`, web/media tools, etc.). But **nothing enforces it at runtime.** The tag
is consulted only by the static `tags_any` matcher an operator could write by hand; there is no
dynamic taint on the processing context, no "once an untrusted result enters this turn, restrict
subsequent write/comm tools," no cross-turn taint persistence. `tool_policy_engine.md` itself admits
dynamic taint tracking is "deferred future work." This is the single most promising foundation for a
real fix to F2/F0: the tags are already in place; what's missing is an enforcement layer that
consumes them (e.g. wrap `OUTPUT_UNTRUSTED` results in a warning banner, or gate which profiles may
forward such output into a [C] tool within the same turn).

### F4 — web search (`brave`) is directly available inside `default_assistant` — **TRADE-OFF (deliberate)**

`default_assistant` can call the `brave` web-search MCP server (`defaults.yaml:455-462`), and can
delegate to `research`/`research_max` which do Google web-grounded synthesis. Both pull open-web
content directly into the fully-trusted [BC] session — first-order [A] into [BC]. Full browser
tooling is *not* here (correctly restricted to browser profiles), but search snippets and research
summaries are untrusted text landing in a [BC] context. This is a conscious utility choice (users
want the assistant to look things up); it should be an *acknowledged* [ABC] surface, and it
interacts badly with the unconfirmed exfil tools in §5.

### F5 — free profile selection via Web/API/A2A — **TRADE-OFF / DEPLOYMENT**

Any authenticated caller can set `profile_id` directly on a chat turn (`chat_api.py:928-941`,
verified) — including `engineer` or `complex_tasks` — with the only check being that the profile is
not `kind: remote`. The same is true of the A2A inbound endpoint. API tokens are **unscoped** (no
per-profile/tool restriction), so a leaked token has the owner's full profile choice. Critically,
the `delegate_to_service → engineer` confirmation gate that `AGENTS.md` implies "always" protects
the engineer profile only fires for **LLM-initiated** delegation; a human or script opening a turn
directly on `engineer` bypasses it. For a single-tenant family deployment this is a defensible
convenience, but it means the confirmation story is weaker than the docs imply and token hygiene
carries more weight than it appears to.

### F6 — notes written by an untrusted-triggered turn are auto-injected into all future turns — **TRADE-OFF (compounding)**

`NotesContextProvider` injects every note whose visibility labels fit the profile's grants into the
system context of **every** turn, no tool call needed. `email_intake` confirm-gates note writes
(good), but `event_handler` does not. Combined with F0/F1, an attacker-triggered turn can write an
unconfirmed note that is then silently baked into `default_assistant`'s prompt on every subsequent
conversation — a durable, no-click injection foothold. This is why F0 is not a one-shot risk: it can
plant persistence.

### F7 — email intake auth is safe-by-config, open-by-default — **DEPLOYMENT**

Out of the box (`enable_actions: false`) nothing runs, which is safe. But once actions are enabled,
`require_authenticated_sender` defaults to **false** (`config_models.py:721`): DKIM/SPF/DMARC
results are computed and stored but **never used to reject**. With the default config, any email
that clears Mailgun's HMAC (i.e. arrived through Mailgun at all) is processed as whatever user its
`From:`/ recipient maps to — the *visible sender is not cryptographically verified*. Similarly, the
Telegram allowlist and the `/webhook/event` ingestion endpoint are **default-open** (empty allowlist
/ empty secrets map = no enforcement). None of these are code bugs; they are unsafe defaults that a
deploying operator must know to close.

### F8 — email auto-reply is sent without confirmation — **TRADE-OFF (low blast radius)**

`email_intake` confirm-gates every write tool, but the pipeline's own auto-generated reply is sent
unconditionally (`email_intake/actions.py:237-260`) — a [C] action derived from LLM output over
untrusted input, with no human in the loop. Blast radius is limited because the recipient is
constrained to the original authenticated sender mapped to that user. But under the permissive
default sender-auth (F7), a spoofed sender could receive a reply that quotes the user's own
calendar/notes.

## 5. Exfiltration & actuation channels (what [C] actually means here)

The severity of any [ABC] seam depends on what outbound channels an injected model can reach. The
worst offenders — all reachable from `default_assistant`, most **without confirmation**:

| Tool                         | Channel                                                                                                            | Confirmation?                                 | Constraint                                                                                                                                                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `call_home_assistant_action` | Arbitrary HA domain/service — `notify.*`, `rest_command.*` (arbitrary outbound HTTP), device control (locks, etc.) | **None**                                      | No domain allowlist. Strongest exfil/actuation path if HA exposes any outbound service.                                                                                                                             |
| `send_message_to_user`       | Message to a chat/conversation ID                                                                                  | **None**                                      | Recipient "must be known" is enforced only in the prompt/description, **not in code** — impl only refuses the current conversation.                                                                                 |
| `mqtt_publish`               | Arbitrary topic/payload to the configured broker                                                                   | **None**                                      | Leaks to any broker subscriber.                                                                                                                                                                                     |
| `download_media`             | Server-side `yt-dlp` fetch of an attacker-shaped URL                                                               | **None**                                      | http/https only, but no host allowlist — weaker than its sibling below.                                                                                                                                             |
| `ingest_document_from_url`   | Server-side URL fetch (exfil via query string)                                                                     | **Confirm** (default_assistant, email_intake) | Gated — the correct pattern.                                                                                                                                                                                        |
| `create_github_issue`        | Arbitrary title/body to GitHub                                                                                     | **Confirm**, engineer-only                    | Gated.                                                                                                                                                                                                              |
| `spawn_worker`               | Isolated coding agent                                                                                              | **None**                                      | No FA data/tools today; but Docker backend defaults to `--network=bridge` (full egress) and no app-created NetworkPolicy, so a worker can reach the network with whatever it's handed. `enabled: false` by default. |
| `ucp_*` shopping             | POST to merchant endpoint                                                                                          | **None**                                      | SSRF constrained by same-site/allowlisted-suffix checks.                                                                                                                                                            |

**The critical interaction:** an injected `default_assistant` turn (via F0, F2, or F4) has *both*
the injection foothold *and* several unconfirmed exfil channels in the same session. The
confirmation gating that protects `email_intake` is exactly what's absent on these tools in the
trusted profiles. `ingest_document_from_url` shows the right pattern (confirm-gated for the same
class of risk); `download_media`, `mqtt_publish`, and `call_home_assistant_action` are the notable
inconsistencies.

## 6. Overall posture and the purity/utility ledger

**Honest summary:** the *architecture* for Rule-of-Two enforcement is real and, in places, genuinely
good — a default-deny policy engine, replay-safe durable confirmations, a properly-contained
`email_intake`, an isolated `engineer`, [AC]-only browser profiles, and capability-monotone scripts.
The *coverage* is incomplete in ways that matter:

1. One outright bug (**F0**) opens a full [ABC] chain reachable by sending the user an email, and
   can plant durable persistence (**F6**). This should be fixed regardless of the broader philosophy
   — it is not a trade-off anyone chose.
2. The largest *deliberate* trade-off is stored untrusted content being freely retrievable by
   trusted profiles (**F2**), with no provenance/taint enforcement (**F3**) and direct web search in
   the trusted profile (**F4**). This is the price of a genuinely useful assistant, and it is
   probably the right call for a single-tenant family tool — **but it means `default_assistant`
   should be honestly understood as an [ABC] profile in practice, not a [BC] one.** Everything then
   rests on the exfil channels in §5 being individually gated, which today they largely are not.
3. Several boundaries are safe-by-config but open-by-default (**F5, F7**), shifting real security
   load onto operator discipline and token hygiene.

The trade-off is defensible; what's missing is that it's **implicit**. The gains (email Q&A, web
lookup, event-driven automation, direct profile access) are real and worth having. The costs are
concentrated in a small number of unconfirmed [C] tools and one profile-routing bug. You do not have
to choose purity over utility wholesale — the highest-leverage moves preserve nearly all the
utility:

- **Fix F0** (route `wake_llm` through profile resolution) and **F1** (tighten `event_handler`).
- **Gate the unconfirmed exfil tools** in §5 to match `ingest_document_from_url`'s confirmation
  pattern — especially `call_home_assistant_action`, `mqtt_publish`, `download_media` — and enforce
  `send_message_to_user`'s recipient constraint in code.
- **Give F3 teeth:** consume the `OUTPUT_UNTRUSTED` tags to restrict or flag [C] tool use within a
  turn that has ingested untrusted content — this is the structural fix for F2/F4 and turns the
  aspirational taint taxonomy into a real control.
- **Flip the dangerous defaults** (F7) or at minimum document them loudly for operators.
- **Decide consciously on F5** — whether unscoped tokens and direct `engineer` selection are
  acceptable for the deployment model.

None of these require abandoning the [BC]-with-strong-auth core design; they close the specific
seams where [A] leaks in and where [C] is ungated once it does.

## 7. Verification notes

- **F0** and **F5** were confirmed by direct code reading (`actions.py`, `task_worker.py`,
  `chat_api.py`), not documentation. F0 was additionally reached independently by two separate
  investigations.
- Remaining findings come from a structured multi-agent audit of entry points, the profile/policy
  engine, the tool inventory, stored-injection paths, and existing mitigations; file:line citations
  are given inline for follow-up.
- This document reflects the state of `defaults.yaml` and `src/family_assistant/` at the time of
  writing and should be revisited when the profile set, tool policy, or event pipeline changes.
