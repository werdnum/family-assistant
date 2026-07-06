# Project Assessment — July 2026

## Status

Point-in-time strategic assessment. Synthesizes a review of the codebase, the open issue backlog,
recent development activity, and a comparison against the two dominant open personal-agent projects
(OpenClaw and Hermes Agent), grounded in observations from real daily usage. Companion artifacts: PR
#989 (note-write confinement), PR #833 (browser credential broker design), and issues #991
(provenance propagation), #992 (source-trust tiers), #993 (turn-level taint).

This is an assessment and direction document, not an implementation design. Individual items get
their own design docs when picked up.

## Where We Stand

Family Assistant is, by mid-2026 standards, feature-complete or ahead in most dimensions that define
the category: roughly ninety local tools across sixteen processing profiles, five interfaces
(Telegram, web, native iOS with voice, email intake, telephony), two browser-automation stacks with
warm-session handoff, UCP commerce, two-tier code execution (sandboxed Monty scripts plus isolated
worker agents), durable cross-interface confirmations, and resumable streaming.

The security architecture — Rule-of-Two processing profiles, the tag-based tool policy engine,
confirmation gating — is genuinely differentiated. The 2026 security record of the popular
alternatives (tens of thousands of exposed instances, a malicious-skill supply-chain campaign,
memory-poisoning research against silent background turns) validates the segregation bet rather than
undermining it.

The most important observation from the comparison: **the rivals' most-loved features are
assemblable from existing primitives here, as configuration rather than code.** OpenClaw's heartbeat
proactivity is one `wake_llm` schedule automation. Hermes's episodic memory is already built
(message history is indexed; `get_message_history` supports semantic and hybrid recall), and its
procedural memory — distilling successful workflows into reusable skills — is a reflection
automation over notes-as-skills, stored scripts, and `test_script_with_simulated_tools`. The
primitives-first design has kept pace with the category without chasing it feature-by-feature.

## Honest Gaps

Real usage shows the assistant performs best in home automation and worst at things like "find that
email from three weeks ago." The pattern behind that is the actual finding:

1. **Ambient context.** The assistant sees only what it is explicitly given. Ingestion is push-only
   — forwarded email via webhook — with no pull-based mailbox sync, so most of the household's
   information stream never enters the (fully built) indexing and retrieval pipeline. Home Assistant
   is the one domain with true ambient visibility (context provider, event stream, state history)
   and is also the domain where the assistant is most useful. The correlation is the diagnosis:
   capability tracks ambient visibility, and proactivity is impossible for an agent that cannot see.

2. **Retrieval is deliberate, never reflexive.** Context providers are conversation-blind
   (`get_context_fragments()` takes no query), so every act of memory requires the model to decide
   to search, spend a tool round-trip, and know what to look for. This fails precisely where it
   matters: voice turns (no latency budget for a search round-trip), background and heartbeat turns
   (the model does not know what it does not know), and casual turns where recall should simply
   work.

3. **Trust granularity.** The Rule of Two is enforced statically, per profile. Two consequences: the
   trusted assistant stays blind to anything untrusted-sourced, and every confined ambient agent is
   a bespoke engineering project — PR #989 spent ~2,100 lines to let one cron job write quarantined
   notes. Most of that was one-time chokepoint work (repository-level write policy, `wake_llm`
   profile routing), but the recurring per-agent cost must fall to configuration-only. Meanwhile the
   `OUTPUT_UNTRUSTED` tool tags exist on ~30 tools with no runtime consumer: the substrate for
   dynamic trust is declared but dormant.

4. **Capability-addition friction.** Adding a capability means dual code-plus-config tool
   registration or an MCP server — the 2025 pattern — rather than the current pattern of a skill
   file plus a CLI available in a sandbox. The skills system and the sandboxed executor both exist;
   what is missing is treating them as the *default* extension path, with the security control moved
   to where it belongs for arbitrary CLIs: network egress, not per-tool policy.

## Strategic Direction

The segregation bet was right; the granularity is wrong. The direction is to move trust enforcement
from per-profile statics to per-turn and per-artifact dynamics:

- **Graduated source-trust tiers at ingestion** (#992): family and known contacts; recognized
  machine senders; unknown humans and arbitrary web content. A turn's taint level is the maximum
  tier present in its context. Most of a family's ambient corpus is low-tier, which is what prevents
  taint-based policy from degenerating into confirm-everything.
- **Turn-level taint with a taint-by-sink policy matrix** (#993): classify sinks by exfiltration
  bandwidth and attacker-addressability. User-facing and home-local sinks stay free at any taint
  level; arbitrary-destination egress (URL fetch, cross-origin navigation, sandbox network) is where
  confirmation and blocking concentrate; broadening reads after high-tier taint escalate to stop
  query-steering. Roll out observe-first: log what the matrix would have gated, tune against
  reality, then enforce.
- **Automatic provenance propagation to written artifacts** (#991): notes, tickets, and automations
  inherit labels from the writing turn's context, attaching at the chokepoints PR #989 built. Stored
  taint stops being a per-case design decision.

Design rule, made explicit after PR #989: **enforcement lives at chokepoints** (repository layer,
context assembly, snapshot layer, egress proxy), **labels propagate automatically, and profiles
shrink to declarations of label policy**. Acceptance test: the next confined ambient agent must cost
only a YAML stanza — new Python beyond a data connector means the propagation layer is not finished.

Threat-model calibration: the realistic adversary for a self-hosted family assistant is scalable
spray-and-pray injection embedded in newsletters, web pages, and mass email — not a targeted
attacker crafting a chain against this specific deployment. Block the cheap generic attack shapes
(attacker-addressable egress after taint, query-steering over the corpus), audit the middle of the
risk spectrum instead of confirm-gating it, and accept exotic low-bandwidth residuals.

## Capability Roadmap

Milestones in intended order; each delivers standalone value. (No calendar estimates, per project
convention.)

1. **Mailbox sync connector.** Pull-based ingestion (Gmail API or IMAP) into the existing indexing
   pipeline, with provenance tiers stamped from day one. Directly fixes the worst observed failure
   ("that email from three weeks ago") and gives ambient and heartbeat turns something to see. The
   connector pattern then generalizes to other pull sources.
2. **Retrieval as reflex.** A query-aware context provider that embeds the incoming turn and injects
   top-k relevant memories (notes, conversations, documents) under existing visibility grants. Small
   primitive, leverage over every turn; multiplies the value of the corpus from milestone 1 and of
   any userspace distillation loops.
3. **Taint machinery** (#992 → #993 → #991), observe-first, then enforced. This is what lets the
   widened ambient corpus flow to the *trusted* assistant instead of quarantined side-agents.
4. **Authenticated browsing, resequenced from PR #833.** First: persistent per-origin browser
   contexts, human-performed login via the existing warm-session handoff, and session-expiry
   detection — this alone unlocks the household site list. In parallel: global snapshot redaction of
   sensitive form values (a chokepoint fix that benefits every profile, not broker infrastructure).
   Then origin-scoped authenticated-session policy (the browser cell of the taint-by-sink matrix).
   The credential broker itself is last and optional; its deferred open question — IdP redirect
   chains (Google/Facebook/Microsoft login) — is the make-or-break for whether it is worth building
   at all.
5. **Skills-plus-CLI as the default extension path.** A network-egress proxy for the sandbox with
   tier-dependent allowlists; in-code tools reserved for capabilities that need per-action policy
   granularity (confirmations, taint tagging). Capability growth stops expanding the policy surface.

Deliberately deprioritized: channel breadth (the rivals' moat, not this project's), a native Android
app (the PWA suffices), and further tool-surface growth (already ahead; diminishing returns against
the agency and memory loops above).

## What We Deliberately Keep

- The Rule of Two and the policy engine — upgraded in granularity, not abandoned.
- Primitives-in-code, behavior-in-configuration. Heartbeats, daily briefs, memory distillation, and
  procedural-skill promotion are userspace automations, not shipped features.
- Secrets never transit the model (PR #833's core idea, kept regardless of broker sequencing).
- UCP-first for commerce; authenticated browsing is the long-tail fallback.
