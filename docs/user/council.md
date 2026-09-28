# The Council

**What's here:** putting a hard question to several AI models at once, what the council does with
it, and what to expect back.

The council is for questions where one careful answer is not enough: a design decision with real
trade-offs, a difficult or speculative question, or broad exploratory work where you want to see the
alternatives as well as a recommendation. Several models work on the question independently, check
each other's work, and a coordinator writes up the result.

It is slow and uses a lot of model time, so it is not the tool for looking something up, routine
questions, or getting code written (use `/coder` for that).

## Asking for one

Ask the Assistant in any chat: "Ask the council whether we should replace the hot water system with
a heat pump or solar", or "Get a council of models to look at this design". Attach any files the
question depends on. The Assistant convenes the council and brings its answer back to you.

The council sees none of your household's notes, calendar, documents or email. Put everything the
question depends on into the request, or attach it.

## Who sits on it

Three models each take a seat: **GPT-6 Sol**, **Claude Fable 5.1** and **Kimi K3**. You can ask for
**Claude Opus 5.5** to sit in place of Fable. Each seat is always the model named: if one is
unavailable, the council says so rather than quietly using a different model. A separate coordinator
runs the process and writes the final answer; it does not vote.

## What happens

1. **Independent proposals.** Each member researches the question on its own — searching the web,
   reading sources, and running code when a calculation would settle something — without seeing the
   others' work.
2. **First review.** Each member reads the other proposals and says what should change: corrections,
   overlooked options, simpler approaches, or better support for an idea.
3. **Second review.** Each member answers the criticism of its own proposal and publishes its final
   position, with what still worries it.
4. **Synthesis.** The coordinator weighs the arguments and evidence — not a vote — and writes up the
   answer.

The Assistant tells you the council has started, and the result arrives in the same chat when it is
done, which can take a while. You can ask for fewer or more review rounds when you ask for the
council.

## What you get back

- The best-supported answer, with its reasoning.
- The alternatives worth knowing about, and when you would choose them instead.
- The uncertainties that could still change the conclusion, and what would settle them.
- Sources, with the ones the coordinator checked itself distinguished from those only a member
  reported.
- Which models took part, and any member that failed.

If a member fails, the council carries on with the other two and says so; with only one left, it
returns what it has as a partial investigation rather than a council result.

## Follow-ups

Asking the council to expand on or explain its answer uses what it has already done; it does not
start again. Ask it to reconvene when something has changed — new evidence, a different constraint —
or when you want it to look again.

## Limits

The council investigates and advises. It does not change anything in your household, send messages,
or publish anything, and it does not save its working as notes.

## Related

- [intelligence-levels.md](intelligence-levels.md) — more thinking from one model, or a quick
  side-by-side of several.
- [slash-commands.md](slash-commands.md) — the other specialised assistants.
