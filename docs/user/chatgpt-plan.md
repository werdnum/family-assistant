# Signing in with ChatGPT

**What's here:** how to let the assistant use a ChatGPT Plus or Pro plan for its OpenAI models, how
to sign in from the web app, and what happens when the plan runs out.

If your household pays for ChatGPT Plus or Pro, the assistant can run its OpenAI models on that
plan's usage instead of a separately billed API key. Once someone signs in, the assistant uses the
plan for every request that goes to an OpenAI model, including scheduled automations. One account
serves the whole household. It never reads your ChatGPT conversations.

## Signing in

The sign-in is in the web app, under **Settings → Connected Accounts**, in the **ChatGPT plan**
card.

1. Choose **Continue with ChatGPT**, then **Open ChatGPT sign-in**. A new tab opens on OpenAI's
   sign-in page.
2. Sign in to ChatGPT and allow Family Assistant to use your ChatGPT plan.
3. The tab then moves to a page that **fails to load**. Its address starts with
   `http://127.0.0.1:1455/auth/callback`. This is expected: the page is only there to carry the
   sign-in result back to you.
4. Copy that page's whole address from the address bar, paste it into the box in Family Assistant,
   and choose **Finish signing in**.

If you take longer than ten minutes, start again.

## Switching accounts and signing out

- **Sign in again with ChatGPT** renews the same account, for example after the card says the
  sign-in has expired.
- **Use a different account** signs in as another ChatGPT account. It replaces the current one.
- **Sign out** stops the assistant using the plan. You can also remove Family Assistant from your
  apps in ChatGPT's own settings.

The sign-in expires if the plan isn't used for 30 days. When that happens, sign in again.

## When the plan runs out

Plans have usage limits. ChatGPT Plus has a five-hour limit that is shared with every other app
using your plan. When the limit is reached, or nobody is signed in, the assistant switches to its
API key if one is set up for that model. If no API key is set up, the request fails with a message
saying why. You can check your usage, and set how much of it Family Assistant may use, under **View
usage in ChatGPT** in the card, which links to ChatGPT's usage settings.
