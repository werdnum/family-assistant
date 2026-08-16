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

| Command                     | Use it for                                                                         |
| --------------------------- | ---------------------------------------------------------------------------------- |
| `/browse`                   | Web tasks needing navigation, forms, logins, or JavaScript-heavy pages             |
| `/browse_visual`            | Sites the DOM-based browser can't handle; drives the page visually                 |
| `/research`                 | In-depth research on a topic — the right default for research questions            |
| `/research_max`             | The most thorough multi-source research, when depth matters more than speed        |
| `/complex`                  | Multi-step reasoning and planning that needs a long chain of work                  |
| `/coder`                    | Writing and running code, data crunching, and other self-contained computing tasks |
| `/visualize` or `/chart`    | Charts and graphs from data you provide or attach                                  |
| `/artist`                   | Generating or editing images and video                                             |
| `/camera` or `/investigate` | Searching and reviewing security camera footage                                    |
| `/engineer`                 | Read-only diagnostics: "why did the assistant do that?"                            |
| `/interrupt`                | (Telegram) stop the request currently being processed in that chat                 |

If you type a command the assistant doesn't recognise, it replies saying so.

## Choosing between them

- **Simple page summaries don't need `/browse`.** The assistant can usually fetch a page directly;
  reach for `/browse` when that fails or the page needs interaction. See
  [research-and-browsing.md](research-and-browsing.md).
- **`/research` for most research; `/research_max` when thoroughness beats turnaround.**
- **`/complex` when a task needs sustained reasoning** over your notes, calendar, documents, and
  other assistant data.
- **`/coder` when you want code written and run, not explained,** and everything it needs is in your
  request. It works in a sandbox described below.
- **`/engineer` for diagnosing the assistant itself,** not for getting work done — it deliberately
  cannot change data or send messages. See [troubleshooting.md](troubleshooting.md).

## Coding and computing (`/coder`)

`/coder` hands the job to a coding agent working in a private Linux sandbox: it writes code, runs
it, reads the files it creates, and searches the web — and keeps going until the task is done rather
than telling you how you might do it yourself.

- `/coder Write a Python script that converts this CSV layout into the JSON shape below, and show me it running on a sample`
- `/coder Work out the compound growth in this table and give me the year-by-year figures`
- `/coder This regex isn't matching the third case — fix it and show me the tests passing`

Three things to know:

- **It cannot see your data.** Your notes, calendar, documents, email, and devices are out of reach
  — it works only from what you put in the request. Use `/complex` when the task needs the
  household's own information.
- **Files you hand it are mounted in its sandbox.** Ask the assistant to pass an attachment along
  and the agent gets the real file to open and rewrite, under `/workspace`.
- **The sandbox is thrown away.** Files it creates live only for that run, so ask it to include
  anything you want to keep in its reply.

One thing it will refuse: a request built out of something the assistant read elsewhere — an email,
a web page — cannot direct it. Anything that runs code takes instructions only from you, so if you
want a task described in a forwarded email carried out, ask for it yourself.

Runs can take a while. Started with `/coder`, the answer arrives in that conversation when it
finishes; handed over by the assistant, you get a `delegation_...` reference and the result is
posted back when it's ready.

## You don't have to use them

The assistant can switch modes on its own when a request calls for it, and will sometimes ask first
("is it okay to use the web browser for this?"). Slash commands are just a way to be explicit.

When a specialist takes a while, the assistant hands you a `delegation_...` reference and keeps the
conversation available; the result is posted back automatically when it's ready. See
[interfaces.md](interfaces.md#delegation-to-specialist-modes).
