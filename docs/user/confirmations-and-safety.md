# Confirmations and Safety

**What's here:** why the assistant asks before doing certain things, how to approve or reject, and
how it handles content that came from outside your household.

## When you'll be asked

Actions with real consequences need your approval first — modifying or deleting a calendar event is
the everyday example, alongside handing work to another mode. Ordinary lookups don't: asking what's
on your calendar or searching your notes just happens.

Which actions are gated depends on your deployment's policy and on how the request reached the
assistant. The same action can go straight through in a direct chat and require approval when it
originated in a forwarded email or followed something the assistant read from an untrusted source.
Writing notes, scheduling reminders, sending messages to other people, and fetching a linked
document all work that way.

Once a turn has taken in untrusted content, even reading can be gated. Searching your documents or
your Gmail widens what the assistant knows while it is holding text someone else wrote, so those
lookups can pause for approval too. It's why a search that normally just runs sometimes asks first.

## Approving

- **Telegram:** inline **✅ Confirm** and **❌ Cancel** buttons.
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

How long an approval stays open depends on the deployment and on which mode asked. Ordinary
assistant confirmations expire after about an hour by default; requests raised from forwarded email
stay open for 24 hours, since you may not be at a trusted interface when the reply lands. If a
confirmation expires before you answer, just ask again.

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

When the assistant hands part of a job to another mode — a browser run, a longer background task —
that mode's actions are usually judged against your original request, not just against the
instruction it was handed. An action the handed-off mode proposes that your request does not account
for is the kind that gets queried or blocked. Work handed on a second time, from one background task
to another, is judged on the narrower ground of the instruction alone, so it errs towards asking.

## Restricted modes

Some situations run the assistant with deliberately reduced powers:

- **Events and webhooks** wake a restricted event-handler mode whose notes are quarantined from your
  main assistant's context. See [automations.md](automations.md).
- **iOS "Capture this"** handles shared pages and emails in a restricted mode that can read your
  information to file things sensibly, but asks before making changes.
- **`/engineer`** inspects the system but cannot change your data or send messages; the code it runs
  runs in a throwaway sandbox with no access to your information, holding only what the assistant
  puts into the command it runs there. See [troubleshooting.md](troubleshooting.md).
- **Scheduled health checks** write only quarantined diagnostics notes. See
  [automations.md](automations.md#scheduled-health-checks).

If one of these says a tool isn't available to it, that's the intended configuration, not a
malfunction.
