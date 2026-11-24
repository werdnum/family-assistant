# Antigravity Reasoning Models as Tiers

## Context

`coder` runs the Antigravity managed agent
([gemini-antigravity-managed-agent.md](gemini-antigravity-managed-agent.md)), which takes its
reasoning model from the caller. The profile was built before model tiers existed
([model-tiers.md](model-tiers.md)), so it pinned that model inline as `antigravity_config.model`
(`gemini-3.8-flash`), and tiers refused Interactions API profiles outright.

The agent now also reasons with `gemini-4-argon`: much stronger, and many times the cost. We want
both on offer, with Flash as the default.

## Decision

**On a profile with `antigravity_config`, a tier names the agent's reasoning model.** The agent is
the runtime, configured by `antigravity_config.agent`; the model is chosen the way every other
profile's is, through `model_tier`, `allowed_model_tiers` and `delegation_model_tiers`. There is one
way to say which model it reasons with, so `antigravity_config.model` is removed.

A tier can mean that only when it is exactly one Google model with no request parameters: the agent
has no fallback and takes no parameters, so anything more would run a recipe other than the one the
run is named for. Startup refuses an Antigravity profile that can reach any other kind of tier,
default or alternate, and refuses `antigravity_config` without a tier or beside an inline model.

`coder` ships as:

- default `gemini_3_8_flash`, a new one-model preset (`standard` minus its fallback);
- `gemini_4_argon` and `gemini_4_neon` selectable by a person (`allowed_model_tiers`) and by a
  delegating model (`delegation_model_tiers`), which the delegation catalog already advertises per
  target;
- no `auto_model_tiers`: Argon and Neon are a choice someone makes, not one the classifier makes on
  cost's behalf.

A second `coder_max` profile was the alternative. It would copy the whole security posture of
`coder` into a second definition and spend context on a second description, for a choice the tier
machinery already expresses.

The council is unaffected: its Gemini seat runs `gemini_4_argon` as a chat model through
`council_member`, not through the agent.

## Consequences

- An Antigravity profile gets its own client per tier (agent id, the tier's model, the profile's
  token cap and sandbox environment) rather than sharing the tier's chat client.
- A delegated run carries its frozen tier envelope into the pollable submit, so a delegation that
  asks for Argon reasons with Argon; polling and cancelling go by interaction id and need no tier.
- A deployment overriding `coder` with `llm_model` or `antigravity_config.model` fails at startup
  and has to move to `antigravity_config.agent` plus a tier.

## Deliberate simplifications

- **Async-run telemetry carries no tier.** The terminal-metrics hook sees an interaction, not its
  delegation run, so an asynchronous run's call record is attributed to the profile alone. Direct
  `/coder` turns go through the ordinary turn path and are attributed by tier. Splitting async spend
  by reasoning model is follow-up work if cost reporting needs it.
- **No Telegram command reaches Argon on `coder`.** Tier commands (`/standard`, `/deep`, `/max`)
  name chat recipes the agent cannot run, so they are refused on this profile; Argon is reached
  through the web tier picker or by asking for it in a delegated request.

## Verification

- Unit tests over the tier rules: each refused tier shape, no tier, an inline agent id.
- Replayed integration runs of the shipped `coder`, built through normal startup
  (`test_shipped_coder_profile_integration.py`): the finished interaction reports the reasoning
  model the backend ran (Flash by default, Argon when a delegation asks for it) and the system
  instruction the agent received.
