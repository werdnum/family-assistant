# Shopping

**What's here:** asking the assistant to find something to buy and hand you a checkout link.

## What it does

Ask for something like "find me a decent cast-iron skillet under $80 and send me a checkout link",
and the assistant will search and browse for candidates, build a cart at the merchant, and give you
the merchant's own checkout URL.

**You complete the payment yourself.** The assistant does not check out on your behalf and never
handles payment details in chat.

## Which shops work

Any merchant supporting the Universal Commerce Protocol (UCP) — Shopify stores and other UCP
merchants alike. The assistant discovers a shop's commerce endpoint from the shop's own published
profile, and notices automatically while browsing when a site supports UCP shopping. You can just
give it the storefront you were looking at.

Some merchants support checkout but not a cart; there the assistant opens a checkout session
directly from the items you picked instead of building a cart first.

If a site publishes no UCP profile, the assistant says so plainly rather than guessing.

## Tips

- Give it constraints it can act on — budget, size, brand, delivery timing.
- Point it at a specific store if you have one in mind; otherwise it will search.
- Check the cart contents in the checkout page before paying. The assistant assembles the cart, but
  the merchant's page is the source of truth.

Checkout handoff requires signing keys configured by your operator; see
[docs/operations/CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md) if handoff
isn't working.
