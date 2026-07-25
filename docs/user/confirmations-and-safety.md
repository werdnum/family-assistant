# Confirmations and Safety

**What's here:** why the assistant asks before doing certain things, how to approve or reject, and
how it handles content that came from outside your household.

## When you'll be asked

Actions with real consequences need your approval first: calendar modifications and deletions,
sending messages or attachments to other people, file operations, and other significant changes of
state. Read-only actions — looking things up, searching, summarising — don't.

## Approving

- **Telegram:** inline **Approve** and **Deny** buttons.
- **Web interface:** a dialog showing what the assistant wants to do, with approve and reject
  options.
- **iOS:** confirmation notifications are actionable — long-press (or pull down) the notification to
  approve or reject from the lock screen, or tap it to open an in-app dialog showing the full
  request.

Pending web approvals are stored durably: reloading the page, or the assistant restarting, doesn't
lose them. If your operator has linked your Telegram and web logins to the same account, an action
requested in Telegram can also be approved in the web interface.

The assistant waits for your answer before proceeding.

## Approvals when you're not there

The assistant sometimes acts without you in the chat — a scheduled reminder firing, an automation
running, a delegated task reporting back. If such a turn needs approval for something, it records
the request as a pending confirmation addressed to you, sends it to your primary channel, and
finishes its turn. The action runs later, once you approve.

Approvals stay open for 24 hours.

## Content from outside your household

Some input can't be trusted the way your own messages can: forwarded email, indexed email
attachments, web pages, documents from unknown senders, and output from external tools. All of these
can contain text written specifically to manipulate the assistant.

Two protections follow:

**The assistant ignores instructions embedded in that content.** Only your direct request drives
what it does.

**The assistant tracks where content came from.** That label follows saved notes, indexed documents,
attachment reads, delegated work, and pending confirmations. When a turn has taken in external
content and then tries to do something consequential — write data, browse a page an attacker could
choose, run networked code, send a message — the action may be audited, require approval, or be
blocked, depending on your operator's policy. This is why you'll sometimes be asked to approve
something that reads as harmless on its own.

## Restricted modes

Some situations run the assistant with deliberately reduced powers:

- **Events and webhooks** wake a restricted event-handler mode whose notes are quarantined from your
  main assistant's context. See [automations.md](automations.md).
- **iOS "Capture this"** handles shared pages and emails in a restricted mode that can read your
  information to file things sensibly, but asks before making changes.
- **`/engineer`** is read-only by design: it can inspect the system but not change data or send
  messages. See [troubleshooting.md](troubleshooting.md).
- **`ops_automation`** can write only quarantined diagnostics notes. See
  [automations.md](automations.md#unattended-operational-diagnostics).

If one of these says a tool isn't available to it, that's the intended configuration, not a
malfunction.
