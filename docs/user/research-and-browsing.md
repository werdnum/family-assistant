# Research and Web Browsing

**What's here:** getting information off the web — quick lookups, page summaries, interactive
browsing, and deep research.

## Quick answers and searches

- "Search the web for reviews of the new park."
- "Who won the game last night?"
- "Find me a recipe for banana bread."
- "What's the weather like today?" / "Will it rain tomorrow in London?"
- "Find coffee shops near me." / "How do I get to the Eiffel Tower?" (if maps are configured)

## Summarising a page

- "Can you summarise this article: `https://example.com/article`?"
- "What's the main point of this webpage: `https://example.com/page`?"

Always give the full address starting with `http://` or `https://`. For a straightforward page the
assistant fetches the content directly — no special command needed.

## Interactive browsing (`/browse`)

When a page needs navigation, forms, logins, or heavy JavaScript, prefix your request with
`/browse`:

- `/browse find recent reviews for the XZ-100 camera and compare its features to the YZ-200`
- `/browse find travel options to Paris for next June`
- `/browse fill out the registration form at example.com`

`/browse_visual` is a fallback that drives the page visually rather than through the DOM, for sites
the standard browser can't handle. During visual browsing, consequential actions — confirming a
payment, sending a message, accepting terms — pause for your approval, as does any page that appears
to contain hidden instructions aimed at hijacking the assistant. You see the proposed action and the
model's explanation; declining stops that action but not the session.

See [browser_automation.md](browser_automation.md) for the full guide.

## Deep research (`/research`, `/research_max`)

`/research` runs a thorough, multi-source investigation and returns a synthesised answer:

- `/research Tell me about the history of Python programming`
- `/research What are the latest developments in renewable energy?`

`/research_max` goes further at the cost of longer turnaround. Use `/research` for most questions
and `/research_max` when thoroughness genuinely matters more than speed.

Research can take a while. When it does, the assistant hands you a delegation reference and keeps
the conversation free; the result is posted back automatically when it's ready, and you can ask a
follow-up that continues the same research session.

## Saving what you find

Ask the assistant to index a page so you can search it later — see
[documents-and-search.md](documents-and-search.md).

## Troubleshooting

- **The page won't load.** Include the full URL with `https://`. Some sites detect and block
  automated browsers; for simple content, try asking without `/browse`.
- **It can't log in somewhere.** The browser session is isolated: it has none of your saved
  passwords or active sessions, cannot solve CAPTCHAs, and can't get past multi-factor
  authentication.
- **The assistant asks before browsing.** It sometimes checks whether it's okay to use the browser
  for a request. That's normal.
