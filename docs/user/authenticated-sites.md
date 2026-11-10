# Websites the assistant can sign into

**What's here:** how to ask the assistant to interact with websites requiring credentials or saved
logins, the difference between on-demand credential browsing and pre-configured authenticated sites,
and what to do when the assistant needs approval or help.

## Two ways to browse with credentials

| Feature                | On-Demand Credential Browsing (`/browse_authenticated`)   | Pre-Configured Authenticated Sites                                |
| ---------------------- | --------------------------------------------------------- | ----------------------------------------------------------------- |
| **Invocation**         | `/browse_authenticated <URL>` or delegation on login wall | Natural conversation ("Check our HelloFresh menu")                |
| **Configuration**      | No assistant configuration; any arbitrary URL             | Configured by operator in YAML (`authenticated_sites`)            |
| **Navigation scope**   | General browsing across domains                           | Strictly confined to configured website origins                   |
| **Session state**      | Ephemeral protected session (discarded after use)         | Persistent saved cookie jar across runs                           |
| **Credentials**        | Keychute autofill requested on demand by secret name      | Pre-saved login session or stored password alias                  |
| **Approval flow**      | Keychute web UI (`/ui/requests/<request_id>`) or grant    | Confirmation prompt before task; Keychute approval on first login |
| **User authorization** | Household users with browsing access                      | Restricted to explicitly named household users                    |

## On-demand credential browsing (`/browse_authenticated`)

Give the assistant a URL and ask it to sign in using Keychute:

> /browse_authenticated Open https://www.amazon.com and check my delivery. If you need to sign in,
> use the Keychute secret `amazon-password`.

You can invoke `/browse_authenticated` directly or start with `/browse` (which delegates to
credential browsing automatically on a login wall). Because credential browsing starts in a separate
protected browser session, cookies, page progress, and element refs from `/browse` do not transfer.
In-page JavaScript (`browser_exec`) and raw page extraction (`browser_extract`) are disabled so page
scripts cannot read back credentials; the browser uses accessibility snapshots and a visual
companion sharing the protected tab instead.

When the assistant reaches a login form, it requests stored credentials via Keychute without needing
prior site configuration:

- **Secret name derivation:** If you name a secret in your request, the assistant requests that
  name. Otherwise it derives a plausible name from the target site or service (such as `octoprint`,
  `3dprinter`, or the domain/app name) rather than stopping to ask you first; you can route the
  request to the right stored secret on the Keychute approval page.
- **Approval in Keychute UI:** Keychute checks the actual HTTPS origin loaded in the browser. When
  approval is required (no standing grant covers the origin), the assistant pauses and shares the
  approval link under `/ui/requests/<id>`. Open the link, verify the origin and secret (rerouting or
  entering a new secret if needed), approve release, and tell the assistant in chat to continue. It
  retries the fill in place without reloading or navigating away.
- **Direct credential delivery:** Keychute delivers the secret directly to the browser backend. Tool
  responses never include secret values and snapshots mask password inputs (note that once an
  approved site receives a password, a hostile page could still echo it into visible page content).
- **Username and multi-step flows:** For username-first logins, the assistant fills the username on
  the first page and the password on the next. If a stored secret holds only a password, the
  assistant enters the known username manually and autofills the password.
- **Rejected passwords:** If the site rejects the credential, the assistant records `bad_password`
  and discards the session immediately to avoid account lockouts. Update the secret in Keychute,
  then ask the assistant to try again in the same conversation.
- **MFA and CAPTCHAs:** Uses a human browser handoff link so you can complete the step in your own
  browser.

Granting a password fill does not confine subsequent navigation in `/browse_authenticated`, so read
the assistant's summary of what it did.

## Pre-configured authenticated sites

Pre-configured sites are operator-defined account presets with a saved login cookie jar (or pinned
password alias) and strict origin confinement, built for recurring household tasks.

### Asking for something

Once a site is configured and you are listed as an authorized user, ask in plain language without
slash commands, URLs, or passwords:

> Pick our HelloFresh meals for next week — something vegetarian on Tuesday.
>
> Check whether our next delivery is still scheduled for Thursday.

You can ask which configured websites you have access to. The assistant asks for your confirmation
before starting or resuming a site task (individual clicks inside the task do not ask again), runs
in a browser already signed in via the saved cookie jar, and reports what it changed.

### Scope and limits

Inside the configured site, the assistant has the same account reach you do when signed in — it can
read account pages, change selections, and submit forms, so a mistake or misleading site prompt can
change real account settings. Keep high-stakes or irreversible actions manual.

It **cannot leave the site**: navigation is locked to the site's configured origins, it cannot read
your notes, calendar, contacts, or smart home devices while running, tool responses never return
passwords, and the browser instance is discarded when the task ends (only the saved cookie jar
persists).

To cut off access immediately, revoke the saved login — that terminates any active session and stops
the stored password from being offered until someone sets it up again.

### When it pauses for you

- **"Take over the browser here: …"** — MFA code, "verify it's you" prompt, or CAPTCHA. Open the
  handoff link, complete the step in that browser, press the button to hand control back, and tell
  the assistant in chat to continue (never paste a one-time code into chat). Note that the page is
  reloaded fresh when the assistant takes control back, so any half-typed form fields are cleared.
  If the handoff expires or the server restarts while waiting, start the task again.
- **"The household has to approve releasing this credential"** — approve the Keychute request, then
  tell the assistant to carry on.
- **"Sign in again and save the login"** — the saved cookie jar expired or was revoked and no stored
  password is configured; sign in and save the login, then ask again.
- **"The stored password needs correcting"** — the site rejected the password, so the assistant
  stopped immediately to avoid lockouts. Fix the stored password and ask again.

### Treating site summaries as untrusted

Summaries report what was written on the site's pages. If a summary instructs you to do something
unexpected (install software, send money, visit an unfamiliar URL), treat that as text from the site
rather than advice from the assistant.

### Operator setup

Adding a site, saving its login, and naming authorized household users require operator
configuration; see [the configuration reference](../operations/CONFIGURATION_REFERENCE.md).
