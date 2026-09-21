# Websites the assistant can sign into

**What's here:** how to ask the assistant to do something on a website your household has a saved
login for, what it can and cannot do there, and what to do when it comes back asking for help.

## Asking for something

Each site has to be set up for you first, with the login saved (or the password stored) and the
people who are allowed to act on that account named. Once it is, you just ask:

> Pick our HelloFresh meals for next week — something vegetarian on Tuesday.

> Check whether our next delivery is still scheduled for Thursday.

Say what you want done, not how to do it. The assistant opens a browser that is already signed in,
works through the site, and reports back what it actually changed. You do not have to tell it which
login to use, hand it a password, or paste a URL — it only knows the sites that were set up, and
only the ones you personally are allowed to use.

The first time you use a newly set-up site, the assistant asks you to confirm before it starts. That
is one confirmation for the whole task, not one per click.

## What it can do there

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
calendar, contacts or home devices while it is working, and it cannot see or repeat any password.
When it is done, the browser is thrown away; only the saved login itself persists.

For anything where a mistake would be expensive or hard to undo, keep doing it yourself.

## Checking and undoing

The assistant reports what it did, so read that rather than assuming. If something is wrong, fix it
the way you normally would on the site — the assistant's changes are ordinary account changes.

If you want to cut off its access entirely, revoke the saved login. That ends any browser session
using it straight away, and it also stops the stored password being offered, so the assistant cannot
quietly sign back in. Setting it up again is a deliberate act.

## When it comes back with a question

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
carry on.

**"Sign in again and save the login"** — the saved login has expired, or was revoked, and there is
no stored password to sign in with. Do the sign-in yourself, save it, and ask again.

**"The stored password needs correcting"** — the site rejected it. Nobody can fix that from inside
the browser session, so the assistant stops rather than trying other passwords. Update the stored
password and ask again.

The assistant will not keep retrying a login that failed, and it will not ask you to type a password
into the chat. If something ever seems to be asking for that, it is not this feature.

## What the assistant reads back to you

The summary comes off the site's own pages, so treat it as a report about that site rather than as
instructions. If a result tells you to do something surprising — install something, send money,
visit another address — that is the site talking, not the assistant, and it is worth being
suspicious of.

## Setting a site up

That part is not something the assistant does. It needs someone with access to the configuration to
add the site, save the login, and decide who may use it. See
[the configuration reference](../operations/CONFIGURATION_REFERENCE.md) for what that involves.
