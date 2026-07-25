# Email

**What's here:** emailing or forwarding mail to the assistant, what it will do on its own, and what
needs your approval.

Email intake has to be configured by your operator. This is about mail sent *to* the assistant; for
searching your own Gmail, see [google-workspace.md](google-workspace.md).

## What it's for

Forward the assistant an order confirmation, a ticket, a travel booking, a school notice, or an
invitation, and ask it to do something with it — or let it summarise the mail and offer to save the
useful parts. A soccer ticket email can become a calendar event or a note. The assistant replies by
email, like a normal chat.

## Why email is treated carefully

A sender address can be forged and email content can contain instructions aimed at the assistant, so
mail is treated as untrusted input. Two rules follow from that:

**The assistant ignores instructions embedded inside forwarded content.** Only your direct request
controls what it does.

**Anything that writes data or sends a message waits for your approval.** Calendar events, notes,
reminders, messages to other Family Assistant users, and fetching a linked document all land as
pending confirmations that you approve in a trusted interface — Telegram or the web UI — before they
run. If Telegram is your configured primary channel, you get inline approval buttons there.
Approvals stay open for 24 hours, so you don't have to react the moment the reply lands.

The assistant replies by email only when your forwarding sender address is mapped to your account by
the operator. Mail arriving through a recipient-only alias without a mapped sender can still create
confirmations, but won't get an email reply.

## Documents linked from an email

If an email points to a document worth keeping — a shared PDF, a Drive or Dropbox or iCloud link, a
long article — the assistant can propose fetching and indexing it. That fetch never happens
immediately: it becomes a confirmation, and only after you approve does the assistant download the
document and add it to your searchable knowledge base. This stops an untrusted sender from pointing
the assistant at arbitrary URLs without your sign-off.

Real MIME attachments on the email are indexed automatically, so the confirmation flow is mainly for
link-style attachments.

## Where email content travels

Summaries, notes, indexed documents, and attachment text that originated in email keep a record of
where they came from. If later work uses that content and then tries to write data, browse a page an
attacker could control, run networked code, or send a message, the assistant may ask for approval
even when the immediate request sounds harmless. See
[confirmations-and-safety.md](confirmations-and-safety.md).
