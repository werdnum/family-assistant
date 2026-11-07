# Intelligence Levels

**What's here:** how to ask for more thinking on a request that deserves it, when it is worth doing,
and what stays the same when you do.

Two separate choices go into every request:

- **Which assistant** you are talking to — the Assistant, the Engineer, the Browser, and so on. This
  decides what it can *do*: which tools it holds, what it can see, how it behaves.
- **How much thinking** to apply to this particular request. This decides how carefully it is
  answered.

The second choice is the intelligence level, and most of the time you should leave it alone.

## The levels

| Level        | Good for                                                                    |
| ------------ | --------------------------------------------------------------------------- |
| **Standard** | Everyday conversation, looking things up, straightforward tasks             |
| **Deep**     | Ambiguity, multi-stage reasoning, difficult judgement, conflicting evidence |
| **Max**      | The hardest requests, where you have decided the extra effort is worth it   |

Higher is not simply better. A stronger level costs more and takes longer, and for a request that
was never difficult it produces the same answer more slowly. Reach for Deep or Max when you can say
why the request is hard: it turns on the answer being *right* rather than merely *produced*.

## Choosing a level

**In the web app and the iOS app**, there is a control beside the assistant picker. Pick a level and
send your message. On iOS it is the gauge button in the Chat toolbar, which shows the level you
picked once you have picked one.

**In Telegram**, put a command at the start of the message (`/standard` takes the Assistant back to
its usual level):

```
/deep Work out whether moving the standing order to the 3rd would ever overdraw the account
/max Read these three quotes and tell me which one is actually cheapest over five years
```

The rest of the message is your request, exactly as you would normally write it.

## It stays for the conversation

An intelligence level, once picked, applies to the message you send it with **and to the rest of
that conversation**, until you pick another. To go back, pick the assistant's usual level (marked
"Default" in the apps; in Telegram, the command for that level).

That is deliberate. Each level runs on a different model, and a model answers fastest and most
cheaply when it has seen the conversation before. Hopping to Deep for one message and straight back
makes both of those messages start cold, so a conversation that has turned hard is better left at
Deep for a while.

The level you are on is shown on the control, and follows the conversation: reopen it later, or on
another device, and it is still there. A new chat, or switching assistants, starts at the usual
level again.

Choosing a level never starts a new conversation and never loses your history. The assistant you
were talking to is still the same assistant, with the same tools and the same access; only how hard
it thinks changes.

## What it does not change

- **What the assistant can do.** A stronger level never grants extra tools, extra access, or
  permission to skip a confirmation. If it needed your approval at Standard, it needs your approval
  at Max.
- **What it can see.** The same notes, calendar and documents, no more.
- **Which assistant answers.** `/deep` runs the same assistant you were already talking to, harder.

## Which assistants offer a choice

The **Assistant** offers all three levels, and so does the **Engineer** (`/engineer`). Both run at
Standard unless you say otherwise; for a hard "why did this happen?" investigation, ask the Engineer
for Deep. **Complex Tasks** (`/complex`) offers Deep and Max — it exists for requests Standard was
not expected to complete well, so Standard is not on its menu.

Every other assistant runs on one fixed level: some because what they do is tied to one model
(reading audio and video, driving a browser visually, running code in a sandbox), the rest because
one level is the right one for the job. Those show no intelligence control, and asking for a level
there is refused with an explanation rather than silently ignored.

## Asking specific models

Sometimes you want a particular model's view rather than a level, or several models' views side by
side: ask the same question of GPT, Claude and Kimi and compare what comes back. Ask the Assistant
in plain words ("put this to GPT-6 Sol, Claude Opus and Kimi K3 and compare their answers"). It
hands the question to a copy of itself running on each model you name, so every answer comes from
the model named, and reports which answer came from which. If one of those models is unavailable,
that answer fails rather than quietly coming from a different model.

You can also pin a specialist to a model in the same way: "have the complex tasks profile work on
this using Gemini 4 Argon" runs that profile, with its usual tools, on the model you named.

The models on offer are the ones your household's setup lists; these are not on the level picker.

A side-by-side is each model answering alone. When you want the models to research the question,
read and criticise each other's answers and revise them before you get a combined answer, ask for a
council instead: see [council.md](council.md).

## Seeing what a reply ran at

In the web app and the iOS app, a reply carries the level it ran at beside the name of the assistant
that produced it. **Which model answered** (the eye button next to the thumbs) adds what was asked
for and the model that actually served it. If you are wondering whether a disappointing answer was
worth re-asking at a higher level, that is where to look first.

Sometimes a reply is labelled **Experiment** instead. That conversation is part of a comparison
between models, and which one answered is hidden on purpose so your thumbs up or down is a fair
judgement. Once you have rated such a reply, **Which model answered** (the eye button next to the
thumbs) shows which model it was. Choosing a level yourself usually takes the conversation out of
the comparison. A comparison can also be set up for one level, such as Frontier: then every reply at
that level is split between the models being compared, including when you chose the level, and your
choice still holds for the rest of the conversation.

## Related

- [slash-commands.md](slash-commands.md) — the commands that switch *which* assistant answers.
- [interfaces.md](interfaces.md) — what Telegram, the web app and the iOS app each offer.
