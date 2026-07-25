# Gmail and Google Drive

**What's here:** connecting your own Google account so the assistant can search and read your Gmail
and Drive, what it can and can't do with them, and how to reconnect or disconnect.

The Google integration has to be enabled by your operator. Configuration for that side lives in
[docs/operations/CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md).

## What it enables

Once connected, you can ask things like:

- "Did the school send the excursion permission form?" — searches your inbox.
- "Show me the email from Jane last week about the lease renewal."
- "Find the tax PDF my accountant shared with me." — searches your Drive.
- "Get the spreadsheet called Family Budget."
- "Draft an email to the school about tomorrow's absence and attach the doctor's note."
- "Save this trip plan as a Google Doc."
- "Upload this PDF to my Family Assistant Drive folder."

The assistant can search your inbox, read full messages (HTML is converted to readable text), fetch
email attachments and return them to you inline, search your Drive, and fetch or export files —
Google Docs, Sheets, and Slides come back as text, other small text files inline, and larger or
binary files as downloadable attachments.

It can also create **unsent** Gmail drafts, optionally attaching existing Family Assistant
attachments, and create or replace files inside its dedicated **Family Assistant** Drive folder.
Authored content becomes a native Google Doc by default; plain text and Markdown are also supported,
and existing attachments can be uploaded as ordinary files.

Gmail searches use standard Gmail syntax (`from:school has:attachment`) and Drive searches use Drive
query syntax (`name contains 'budget'`), so you can be as precise as you like.

## What it cannot do

- **Send email.** Drafts stay unsent for you to review and send from Gmail; there is no send
  operation at all.
- **Delete your Google data.**
- **Write outside its own folder.** Drive writes are confined to the app-created **Family
  Assistant** folder and app-created files within it. Creating a file with an existing name is
  refused unless the assistant explicitly asks to overwrite, and even then it can only replace an
  app-created file of the same type in that folder.
- **See anyone else's account.** Each connection is strictly the connecting user's own mailbox and
  Drive.

## Mind the audience

The assistant replies wherever you asked. If you ask about your email in a group chat, the answer —
and any attachment it fetches — is visible to everyone in that chat. It's instructed to prefer
summaries and offer private delivery for personal content in shared conversations, but the simplest
rule is to ask about private matters in a direct chat.

## Connecting

1. Go to **Settings → Connected Accounts** in the web interface.
2. Click **Connect Google account**.
3. Sign in on Google's consent screen with the account you want to connect, and approve the
   requested permissions.
4. You're returned to Connected Accounts, and a notification names the account that was linked. If
   you ever see an account you don't recognise, disconnect it immediately — one click from the same
   page.

You only need to connect once, in the web interface. The assistant then uses that connection from
any interface.

**Partial grants.** Google's consent screen lets you approve some permissions and not others (Gmail
but not Drive, say). That's fine — the assistant tells you if you try to use a feature whose
permission you didn't grant, and Connected Accounts shows which permissions are missing and prompts
you to reconnect. Draft creation and Drive writing each need their own permission; if you connected
before those features existed, use **Reconnect** and approve the new permissions.

## Reconnecting and disconnecting

- **Reconnect** — replaces the existing connection with the new consent you give. A notification
  confirms which account is now linked.
- **Disconnect** — removes the connection; the assistant loses access to your Gmail and Drive until
  you reconnect.

## "Needs re-authorization" notifications

Occasionally you'll get a notification saying your Google connection needs renewing. This happens
when Google revokes the stored authorization — after a password change, for example. Go to
**Settings → Connected Accounts** and click **Reconnect**.

If everyone in the household gets these on a regular cycle, the deployment's OAuth client is
probably still in testing mode; point your operator at the Google integration section of
[CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md).
