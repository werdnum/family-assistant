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

**Only what you typed counts as your request.** When the mail comes from your own address and passes
the sender authentication your operator requires, the assistant reads the words you wrote yourself
as your request. Everything else in the message is just material: the conversation you replied to,
the message you forwarded, your signature, and attachments. Instructions inside that material are
ignored. This follows Gmail's normal reply and forward layout, so type your request above the quoted
or forwarded part. A subject line you wrote counts too, but a "Re:" or "Fwd:" subject doesn't.

**Writes and messages are checked before they run.** Calendar events, notes, reminders, messages to
other Family Assistant users, and fetching a linked document are checked automatically against what
you asked for. When the check is satisfied it goes ahead. Otherwise it lands as a pending
confirmation for you to approve in a trusted interface: Telegram, the web UI, or the iOS app. A
forward with nothing typed above it has no request of yours to check against, so whatever it
proposes waits for your approval. If Telegram is your configured primary channel, you get inline
approval buttons there. Approvals stay open for 24 hours, so you don't have to react the moment the
reply lands.

The assistant replies by email only when your forwarding sender address is mapped to your account by
the operator. Mail arriving through a recipient-only alias without a mapped sender can still create
confirmations, but won't get an email reply.

## Documents linked from an email

If an email points to a document worth keeping — a shared PDF, a Drive or Dropbox or iCloud link, a
long article — the assistant can fetch and index it. Say so in your own words ("save the PDF linked
below") and the fetch is checked against that request; otherwise it waits for your approval. This
stops an untrusted sender from pointing the assistant at arbitrary URLs without your sign-off.

Real MIME attachments on the email are indexed automatically, so the confirmation flow is mainly for
link-style attachments.

## Where email content travels

Summaries, notes, indexed documents, and attachment text that originated in email keep a record of
where they came from. If later work uses that content and then tries to write data, browse a page an
attacker could control, run networked code, or send a message, the assistant may ask for approval
even when the immediate request sounds harmless. See
[confirmations-and-safety.md](confirmations-and-safety.md).
