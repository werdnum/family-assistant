# Troubleshooting

**What's here:** what to do when the assistant isn't doing what you expect, and how to report a
problem.

Topic-specific troubleshooting lives in each guide — [calendar.md](calendar.md),
[automations.md](automations.md), [smart-home.md](smart-home.md),
[research-and-browsing.md](research-and-browsing.md), [media.md](media.md),
[camera_integration.md](camera_integration.md), [data_visualization.md](data_visualization.md). This
page covers the general cases.

## The assistant isn't responding

1. Check your own connection.
2. Send a simple message ("hello") to test basic communication.
3. Give it a moment — complex requests, especially research and browsing, take time.
4. In Telegram, check notifications are enabled for the bot.
5. Try the other interface. If Telegram is quiet, the web interface may still work.

## It misunderstood you

- **Rephrase.** Slightly different wording often makes a large difference.
- **Be specific.** "Remind me to pick up dry cleaning at 5pm tomorrow" beats "remind me about dry
  cleaning".
- **Correct it directly.** "Actually, the appointment is at 3 PM" is enough; you don't need to start
  over.
- **Fix the underlying fact.** If it's working from a stale note, update the note — in conversation
  or on the **Notes** page.
- **Reply to the specific message** in Telegram when following up, so it knows what you're referring
  to. This also keeps the conversation in a specialised mode if you were using one.

## Starting fresh

Each conversation carries its own context. In the web interface and iOS app, start a new
conversation. In Telegram, start a new conversation or let the context age out.

## It switched modes, or asked to

The assistant may move to a specialised mode for a request, or ask permission first ("is it okay to
use the web browser for this?"). This is normal — it's picking better tools for the job. See
[slash-commands.md](slash-commands.md).

## Something is waiting on you

Actions with consequences pause for approval and don't run until you respond. If a change you asked
for hasn't happened, check for a pending confirmation in Telegram, the web interface, or your iOS
notifications. See [confirmations-and-safety.md](confirmations-and-safety.md).

## A background task failed

Open the **Tasks** page in the web interface. It shows scheduled and background work with error
messages, and lets you retry a failed task manually.

## Unknown commands

If you type a slash command the assistant doesn't recognise, it replies telling you so. See
[slash-commands.md](slash-commands.md) for the list.

## Reporting a bug

If something seems broken — a tool errors out, data looks wrong, the assistant can't do something it
should — just say "report this as a bug". The assistant may also file one on its own when it notices
a problem. Say what you were trying to do and what happened instead.

This records the issue in the application's error log, where whoever manages the assistant can
review it on the web **Error Logs** page (`/errors`). Filing a report doesn't fix the problem by
itself.

The iOS app reports its own errors automatically, so an error you saw on your phone is already
visible to your administrator without you having to describe it. Crashes are reported through
Apple/TestFlight.

## Diagnosing the assistant itself (`/engineer`)

For "why did the assistant do that?" questions, `/engineer` switches to a read-only diagnostic mode.
It can read the application's source code, query its database, inspect error logs and configuration,
and explain which tools each mode may use — including why a particular tool call was allowed,
denied, or required confirmation.

It deliberately cannot change data or send messages. Every action with a side effect — filing a
GitHub issue, reconnecting an MCP server, launching or cancelling an isolated coding worker, handing
work to another mode — asks for your approval first. If it tells you a tool isn't available to it,
that's the intended safety configuration, not a fault.

## Still stuck

Contact the person who set up and manages Family Assistant for your household. Configuration
problems, missing integrations, and anything requiring server access are theirs to fix; operator
documentation lives in
[docs/operations/CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md).
