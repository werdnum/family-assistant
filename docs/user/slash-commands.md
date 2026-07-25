# Slash Commands and Specialist Modes

**What's here:** the commands that switch the assistant into a specialised mode, and when to reach
for each.

**These are a Telegram feature.** In Telegram, put the command at the start of your message,
followed by your request:

```
/research Compare the regulatory landscape for autonomous vehicles across the EU, US, and Japan
```

In the web interface and the iOS app there is no command prefix — pick the profile you want from the
profile picker instead, and then type your request normally. Typing `/research …` into a web or iOS
chat sends it as ordinary text and does not switch modes.

Which commands exist depends on how your deployment is configured; these are the standard ones.

| Command                     | Use it for                                                                  |
| --------------------------- | --------------------------------------------------------------------------- |
| `/browse`                   | Web tasks needing navigation, forms, logins, or JavaScript-heavy pages      |
| `/browse_visual`            | Sites the DOM-based browser can't handle; drives the page visually          |
| `/research`                 | In-depth research on a topic — the right default for research questions     |
| `/research_max`             | The most thorough multi-source research, when depth matters more than speed |
| `/complex`                  | Multi-step reasoning and planning that needs a long chain of work           |
| `/visualize` or `/chart`    | Charts and graphs from data you provide or attach                           |
| `/artist`                   | Generating or editing images and video                                      |
| `/automate`                 | Creating and validating automations                                         |
| `/camera` or `/investigate` | Searching and reviewing security camera footage                             |
| `/engineer`                 | Read-only diagnostics: "why did the assistant do that?"                     |
| `/interrupt`                | (Telegram) stop the request currently being processed in that chat          |

If you type a command the assistant doesn't recognise, it replies saying so.

## Choosing between them

- **Simple page summaries don't need `/browse`.** The assistant can usually fetch a page directly;
  reach for `/browse` when that fails or the page needs interaction. See
  [research-and-browsing.md](research-and-browsing.md).
- **`/research` for most research; `/research_max` when thoroughness beats turnaround.**
- **`/complex` when a task needs sustained reasoning** over your notes, calendar, documents, and
  other assistant data.
- **`/engineer` for diagnosing the assistant itself,** not for getting work done — it deliberately
  cannot change data or send messages. See [troubleshooting.md](troubleshooting.md).

## You don't have to use them

The assistant can switch modes on its own when a request calls for it, and will sometimes ask first
("is it okay to use the web browser for this?"). Slash commands are just a way to be explicit.

When a specialist takes a while, the assistant hands you a `delegation_...` reference and keeps the
conversation available; the result is posted back automatically when it's ready. See
[interfaces.md](interfaces.md#delegation-to-specialist-modes).
