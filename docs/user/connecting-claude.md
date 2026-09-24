# Connecting Claude

**What's here:** how to let Claude — on the web, in Claude Code, or through the Claude API — ask
your Family Assistant questions and pass it requests, what that connection can and cannot do, and
how to disconnect it.

## What the connection does

Once connected, Claude gains one tool: **ask Family Assistant**. When your conversation with Claude
touches something the household assistant knows — "what's on the family calendar this weekend?",
"add milk to the shopping list", "what did we decide about the plumber?" — Claude passes the
question on, the assistant answers as it would if you had typed it yourself, and the reply comes
back into your Claude conversation.

Everything happens as *you*. The assistant sees your notes, your calendars and your documents, and
anything it does — a note saved, an event added — is done in your name, exactly as if you had asked
from Telegram or the web app. Whoever runs your Family Assistant chooses which of the assistant's
modes answers these requests; usually it is the ordinary assistant.

Claude decides when to use the tool, so you can just talk to it naturally. Saying "ask my family
assistant…" makes it explicit when you want to be sure.

## Continuing a conversation

Each question Claude passes on starts a conversation in Family Assistant, and Claude keeps hold of
its reference. Follow-ups in the same Claude chat continue that conversation, so "and what about
Sunday?" means what you'd expect. The conversation also shows up in **History** in the web app,
where you can read what was asked and answered; to carry it on, keep talking to Claude.

## Approving actions

Some things the assistant does need your approval first — changing a calendar event, say, or
anything your deployment's policy gates. See
[confirmations-and-safety.md](confirmations-and-safety.md) for which actions those are.

A request that arrives through Claude cannot be approved *inside* Claude. Instead the assistant
records a pending approval, tells Claude that it is waiting for you, and notifies you on your usual
channel — Telegram, a push notification, or the web app. Approve or reject it there, and the action
runs. This is the same way approvals work when you ask through Siri: the request is kept safe until
you get to a place where you can answer it, and it expires if you never do.

Requests arriving through Claude are treated as coming from a program acting for you rather than
from you directly, so a deployment may ask for approval a little more readily than it would in a
direct chat. If Claude reports that the assistant is waiting for approval, look for the pending
request in the web app or Telegram.

## Connecting from Claude on the web

This adds Family Assistant as a **custom connector** in claude.ai (also used by the Claude desktop
and mobile apps). You need the address of your Family Assistant — the same `https://…` address you
use for the web app — from whoever runs it.

1. In Claude, open **Customize → Connectors** (organization owners on a Team or Enterprise plan:
   **Organization settings → Connectors**) and choose **Add custom connector**.
2. Enter your Family Assistant address followed by `/api/mcp`, for example
   `https://assistant.example.com/api/mcp`. Leave the OAuth client ID and client secret blank —
   Claude registers itself with Family Assistant automatically — unless whoever runs your Family
   Assistant gave you a client ID for Claude. In that case, open **Advanced settings**, enter the
   client ID, and leave the secret blank.
3. Click **Add**, then find the connector in the list and click **Connect**.
4. Claude sends you to sign in — to Family Assistant, or to your household sign-in page if you were
   given a client ID. Sign in the way you normally sign in to the web app if you are not already,
   then approve the page asking whether to let Claude act on your behalf. You are returned to
   Claude, connected.

To use it in a chat, open the **+** menu, choose **Connectors**, and make sure Family Assistant is
switched on for that conversation.

Free plans allow one custom connector; paid plans allow several.

## Connecting from Claude Code

Claude Code connects with a personal API token.

1. In the Family Assistant web app, open **Settings → API Tokens** and create a token. Copy it; it
   is shown once.

2. Add the server, substituting your address and token:

   ```bash
   claude mcp add --transport http family-assistant https://assistant.example.com/api/mcp \
     --header "Authorization: Bearer YOUR_TOKEN"
   ```

   Add `--scope user` to make it available in every project rather than just the current one.

3. In a Claude Code session, `/mcp` shows the server and its tool. Ask away.

Claude Code can also sign in through the browser instead of using a token: add the server without
`--header`, then run `/mcp` (or `claude mcp login family-assistant`) and follow the sign-in and
approval steps described above for the web. If whoever runs your Family Assistant gave you a client
ID for Claude Code, include it when adding the server:

```bash
claude mcp add --transport http --client-id THE_CLIENT_ID --callback-port 8765 \
  family-assistant https://assistant.example.com/api/mcp
```

Some deployments accept only this browser sign-in from outside the home network, in which case the
token method above works only at home.

## Connecting from the Claude API

If you are writing your own program on the Claude API, the Messages API can call Family Assistant
directly through its MCP connector. Create a token under **Settings → API Tokens** as above, then
name the server and its toolset in the request:

```python
client.beta.messages.create(
    model="claude-opus-5",
    max_tokens=1000,
    messages=[{"role": "user", "content": "What's on the family calendar this weekend?"}],
    mcp_servers=[
        {
            "type": "url",
            "url": "https://assistant.example.com/api/mcp",
            "name": "family-assistant",
            "authorization_token": "YOUR_TOKEN",
        }
    ],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "family-assistant"}],
    betas=["mcp-client-2025-11-20"],
)
```

## Disconnecting

- **Claude on the web:** if you connected with a client ID, the connection lives with your household
  sign-in instead: remove it from your account's applications page there, and remove the connector
  in Claude. Otherwise the connection appears in Family Assistant under **Settings → API Tokens**,
  named after the connector. Revoke it there and Claude is disconnected the next time it tries to
  ask; remove the connector in Claude's **Customize → Connectors** too so it stops trying. A
  connection made this way only lets Claude ask the assistant — it cannot be used to reach anything
  else in Family Assistant.
- **Claude Code and the API:** revoke the token under **Settings → API Tokens**. A personal token
  gives access to everything you can do in the web app, not just asking the assistant, so keep it
  private and revoke it when you stop using it. `claude mcp remove family-assistant` takes the
  server out of Claude Code.

If any of these steps fail — the connector won't connect, or Claude reports it cannot reach the
assistant — the connection has to be enabled and set up by whoever runs your Family Assistant. Ask
them.
