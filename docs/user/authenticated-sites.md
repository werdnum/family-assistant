# Websites the assistant can sign into

**What's here:** how to ask the assistant to interact with websites requiring credentials or saved
logins, the difference between on-demand credential browsing and pre-configured authenticated sites,
and what to do when the assistant needs approval or help.

## Two ways to browse with credentials

Family Assistant supports two distinct ways to work with websites that require authentication:

1. **On-demand credential browsing (`/browse_authenticated`)**: Ad-hoc browsing at arbitrary URLs
   using passwords stored in Keychute. No prior assistant site setup is needed; Keychute handles
   interactive approval and autofills credentials directly into the browser.
2. **Pre-configured authenticated sites**: Operator-configured presets for specific services (e.g.
   HelloFresh, utility portals). These restrict the browser to specific web origins, maintain saved
   login cookie jars, and control which household members are allowed to act on the account.

| Feature                | On-Demand Credential Browsing (`/browse_authenticated`)   | Pre-Configured Authenticated Sites                                |
| ---------------------- | --------------------------------------------------------- | ----------------------------------------------------------------- |
| **Invocation**         | `/browse_authenticated <URL>` or delegation on login wall | Natural conversation ("Check our HelloFresh menu")                |
| **Configuration**      | No assistant configuration; any arbitrary URL             | Configured by operator in YAML (`authenticated_sites`)            |
| **Navigation scope**   | General browsing; can navigate across domains             | Strictly confined to configured website origins                   |
| **Session state**      | Ephemeral protected session (discarded after use)         | Persistent saved cookie jar across runs                           |
| **Credentials**        | Keychute autofill requested on demand by secret name      | Pre-saved login session or stored password alias                  |
| **Approval flow**      | Keychute web UI (`/ui/requests/<request_id>`) or grant    | Confirmation prompt before task; Keychute approval on first login |
| **User authorization** | Available to household users with browsing access         | Restricted to explicitly named household users                    |

______________________________________________________________________

## On-demand credential browsing (`/browse_authenticated`)

You can give the assistant a URL and ask it to sign in using Keychute:

> /browse_authenticated Open https://www.amazon.com and check my delivery. If you need to sign in,
> use the Keychute secret `amazon-password`.

For login tasks, the assistant uses a dedicated, protected browser session. You can invoke it
explicitly with `/browse_authenticated` or start with `/browse`; if ordinary browsing encounters a
login wall, the assistant can switch to credential browsing automatically. Because the credential
browser starts in a separate protected session to keep credentials safe, ordinary browsing cookies,
page progress, and element refs do not transfer. Ordinary browsing allows JavaScript execution and
raw page extraction, while credential browsing disables those capabilities and relies on page
snapshots and visual actions instead.

No site configuration or standing grant is needed beforehand. When the assistant reaches a login
form, it requests stored credentials:

- **Synthesizing secret names**: If you specify a secret name in your request, the assistant uses
  it. If you do not specify a secret name, the assistant does not pause to ask you; it derives a
  plausible, sensible name based on the target site or service (e.g. `octoprint`, `3dprinter`, or
  the domain/app name). You can route the request to the correct secret directly in Keychute during
  approval.
- **Approval in Keychute UI**: If approval is needed, the assistant pauses and gives you the
  Keychute web UI approval link, which lives under `/ui/requests/<id>`. Open the link, review the
  destination origin and requested secret, route it if necessary, and approve it. Then tell the
  assistant to continue in your chat. You can also configure a standing grant in Keychute to permit
  future requests on trusted origins automatically.
- **Direct credential delivery**: Keychute injects the credential directly into the browser. Tool
  responses do not return secret values, and page snapshots mask password controls. Note that
  read-back protection against an approved site is best effort: once an approved site receives a
  password, a hostile or compromised page could potentially echo it into visible page content.
- **Password-only secrets and manual username entry**: If a released secret contains only a password
  without a username, the assistant can enter the known username manually and autofill only the
  password. For multi-step logins, it fills the username on the first page and the password on the
  next.
- **Failed passwords**: If the site rejects the credentials, the assistant records the rejection,
  immediately discards the browser session to prevent account lockouts, and asks you to correct the
  stored secret. Once corrected, ask the assistant to try again in the same conversation.
- **Handoff for MFA or CAPTCHA**: For multi-factor codes or CAPTCHAs, the assistant provides a
  browser handoff link so you can complete the challenge in your own browser.

This is general browsing: the assistant can navigate between websites and perform actions on
accounts you approve. Granting a password fill does not restrict subsequent navigation. Read its
report of what it did.

______________________________________________________________________

## Pre-configured authenticated sites

Pre-configured sites are operator-defined account presets. They supply a saved login (or pinned
password alias) and lock browsing strictly to an allowed set of web addresses. They are designed for
recurring household workflows and do not require slash commands or explicit URLs.

### Asking for something

Each site has to be set up in the assistant configuration first, with the login saved (or the
password stored) and the people who are allowed to act on that account named. Once it is, you just
ask naturally:

> Pick our HelloFresh meals for next week — something vegetarian on Tuesday.
>
> Check whether our next delivery is still scheduled for Thursday.

Say what you want done, not how to do it. The assistant opens a browser that is already signed in
using the saved cookie jar, works through the site, and reports back what it actually changed. You
do not have to tell it which login to use, hand it a password, or paste a URL — it only knows the
sites that were set up, and only the ones you personally are allowed to use.

You can ask which websites are available; the assistant sees only sites you are authorized to use.

The assistant asks you to confirm before starting or resuming a site task. Individual browser clicks
do not need separate confirmation.

### What it can do there

Inside a configured site, the assistant has the same reach you do when you are signed in. It can
read your account, change selections, and submit forms. That is the point, and it is also the honest
limit: it is a general browser, not a narrow tool that can only do one safe thing.

So when you enable a site, you are accepting that the assistant might:

- choose things you would not have chosen;
- change a setting you liked;
- act on a misleading instruction the site itself displays;
- submit or agree to something you did not intend.

It cannot leave the site. The browser is locked to that site's own web addresses, so nothing it
reads there can send it to your bank, your email, or another saved login. It cannot read your notes,
calendar, contacts or home devices while it is working, and tool responses do not return stored
passwords. When it is done, the browser is thrown away; only the saved login itself persists.

For anything where a mistake would be expensive or hard to undo, keep doing it yourself.

### Checking and undoing

The assistant reports what it did, so read that rather than assuming. If something is wrong, fix it
the way you normally would on the site — the assistant's changes are ordinary account changes.

If you want to cut off its access entirely, revoke the saved login. That ends any browser session
using it straight away, and it also stops the stored password being offered, so the assistant cannot
quietly sign back in. Setting it up again is a deliberate act.

### When it comes back with a question

Some steps need you. The assistant will say which, and the task waits for you rather than guessing.

**"Take over the browser here: …"** — the site asked for something only you can do: a one-time code,
a "verify it's you" tap, a captcha. Open the link, finish that step in the browser that appears, and
press the button on that page to hand it back. Then tell the assistant to carry on: it picks the
browser back up by itself, still signed in and still locked to the same site. There is no code to
copy out of that page and paste into the chat — if you are asked for one, that is not this feature.
The page you were on is reloaded fresh when the assistant takes over, so anything half-typed into a
form is lost — worth knowing if you were mid-way through something.

If you have not handed it back yet, the assistant simply says the task is still waiting; ask again
once you are done. If you leave it long enough for the browser to be closed or to expire, the task
ends and has to be started again — it will not quietly open a new browser in its place. A task
waiting for you also ends if the assistant service restarts; ask it to start the task again.

**"The household has to approve releasing this credential"** — the assistant needs to use your
stored password and the approval has not come through yet. Approve it, then tell the assistant to
carry on. If you deny the request or it expires, the task stops waiting for that approval; you can
ask for a new task when you are ready.

**"Sign in again and save the login"** — the saved login has expired, or was revoked, and there is
no stored password to sign in with. Do the sign-in yourself, save it, and ask again.

**"The stored password needs correcting"** — the site rejected it. Nobody can fix that from inside
the browser session, so the assistant stops rather than trying other passwords. Update the stored
password and ask again.

The assistant will not keep retrying a login that failed, and it will not ask you to type a password
into the chat. If something ever seems to be asking for that, it is not this feature.

### What the assistant reads back to you

The summary comes off the site's own pages, so treat it as a report about that site rather than as
instructions. If a result tells you to do something surprising — install something, send money,
visit another address — that is the site talking, not the assistant, and it is worth being
suspicious of.

### Setting a site up

That part is not something the assistant does. It needs someone with access to the configuration to
add the site, save the login, and decide who may use it. See
[the configuration reference](../operations/CONFIGURATION_REFERENCE.md) for what that involves.
