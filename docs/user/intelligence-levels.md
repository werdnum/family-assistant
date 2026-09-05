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

**In the web and iOS apps**, there is a control beside the assistant picker. Pick a level and send
your message.

**In Telegram**, put a command at the start of the message:

```
/deep Work out whether moving the standing order to the 3rd would ever overdraw the account
/max Read these three quotes and tell me which one is actually cheapest over five years
```

The rest of the message is your request, exactly as you would normally write it.

## It applies to one request

An intelligence level is **one-shot**: it applies to the message you send it with, and the next
message goes back to the assistant's usual level. That is deliberate — a hard question in the middle
of an ordinary conversation is a property of that question, not of the conversation.

The web and iOS apps also let you pin a level for the current conversation, when you know the whole
conversation is going to be hard. The pin is visible while it is on, and clears when you start a new
chat or switch assistants.

Choosing a level never starts a new conversation and never loses your history. The assistant you
were talking to is still the same assistant, with the same tools and the same access; only how hard
it thinks changes.

## Asking for more thinking in words

You can also just say so — "think carefully about this", "this one is subtle" — and the assistant
takes it into account. Words are a hint; the control and the commands are a decision. When it
matters, use the control.

## What it does not change

- **What the assistant can do.** A stronger level never grants extra tools, extra access, or
  permission to skip a confirmation. If it needed your approval at Standard, it needs your approval
  at Max.
- **What it can see.** The same notes, calendar and documents, no more.
- **Which assistant answers.** `/deep` on a conversation with the Engineer is still the Engineer.

## Not every assistant offers a choice

Some are tied to one model because of what they do — the ones that read audio and video, drive a
browser visually, or run code in a sandbox. Those show no intelligence control, and asking for a
level there is refused with an explanation rather than silently ignored.

## Seeing what a reply ran at

Message details show the level a reply was produced at, what was asked for, and the model that
actually served it. If you are wondering whether a disappointing answer was worth re-asking at a
higher level, that is where to look first.

## Related

- [slash-commands.md](slash-commands.md) — the commands that switch *which* assistant answers.
- [interfaces.md](interfaces.md) — what Telegram, the web app and the iOS app each offer.
