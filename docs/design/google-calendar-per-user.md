# Per-User Google Calendar

## Status

Implemented.

## Problem

Calendars are deployment-scoped: the operator configures one set of CalDAV collections and iCal
feeds, and every user sees the same ones. Household members who live in their own Google Calendar
cannot ask the assistant about it or add to it. The per-user Google connection built for Gmail and
Drive ([user-scoped-google-data-access.md](user-scoped-google-data-access.md)) already holds an
OAuth grant per user, so Google Calendar can ride on it as an additional scope.

## Approach

Google Calendar is not a new set of tools. Each user's Google calendars become **calendar sources**
of the acting user, alongside the configured CalDAV and iCal sources, so the existing calendar tools
and the calendar context provider serve them.

- **Scopes.** `calendar.readonly` enables reading (the calendar list exists only under it);
  `calendar.events` additionally enables writes. Both are in the supported allowlist and the default
  scope list. Users who connected earlier keep Gmail/Drive and reconnect to grant calendar access,
  through the existing partial-grant path.
- **Source identity.** A Google calendar's source id is `google:<calendarId>`, with the primary
  calendar as `google:primary`. The id carries the real calendar id, so a write tool can address a
  calendar without listing calendars first, and the acting user's token is what limits it to
  calendars that user can reach.
- **Which calendars.** The primary calendar goes into the per-turn calendar context. The other
  calendars are reached only through the tools: a search that names no calendars covers the ones the
  user has visible in Google Calendar (`selected` and not `hidden`), and hidden calendars are
  searched when named. The configured CalDAV default remains the default target for new events;
  `google:primary` is the default only where no CalDAV calendar is configured.
- **Acting user only.** Every Google call resolves the token for the turn's acting user, as the
  Gmail/Drive tools do. Tools bind to the execution context. Context providers had no notion of a
  user, so `get_context_fragments` now receives the turn's acting user; providers of deployment-wide
  data ignore it. The credential resolver gains a user-id entry point for that caller, documented as
  taking the turn's acting user and never a model-supplied value. One shared request path
  (`GoogleUserApi`) serves Gmail, Drive and Calendar.
- **No outbound mail.** Writes never set attendees and always pass `sendUpdates=none`, so the write
  tools stay `artifact_write` sinks rather than external communication.

## Taint

Calendar content is attacker-addressable: anyone can send an invitation, and Google adds it to the
calendar before it is answered. Events Google extracts from email are built from email content.

- **Tools.** `search_calendar_events` is already tagged untrusted, so Google results taint the turn
  as iCal results do. A calendar list that includes a calendar owned by another account taints the
  turn, since its name is theirs, and a duplicate-detection warning quoting a Google event does too,
  as does a modify or delete confirming the title of an event the user did not create or accept.
- **Context.** The per-turn context is untainted, like the CalDAV context it extends, so it shows
  only events the user put there: ones they created or organise, and invitations they accepted.
  Unanswered or declined invitations, invitations addressed to a group the user belongs to,
  Gmail-extracted events, and events someone with edit access added under their own name are left
  out and remain findable by search, where they carry taint. Tainting every turn of every connected
  user instead would put the confirmation gates on everything that user does.
- **Floor.** The integration's taint-floor startup check covered profiles allowing a Gmail/Drive
  tool. With calendar access configured the calendar tools reach Google data too, so profiles
  allowing them are checked as well. They stay registered whether or not Google is enabled: the
  integration never filters them, it only extends the set of profiles the floor applies to.

## Failure behaviour

- Not connected, declined calendar access, or no acting user: the context simply has no Google
  events. The tools omit Google quietly in an unfiltered search; `list_calendars`, and a search that
  names a Google calendar, say why Google is missing.
- Any other failure (needs re-authorization, API error, transport error) is reported: in the context
  as a note under the events, in a search as a note naming the calendar that could not be read,
  while the other calendars' results are still returned.

## Deliberate simplifications

- **Accepted invitations count as the user's own.** An accepted invitation's title is still written
  by its organizer; accepting it is treated as vetting it, like a forwarded message from a known
  contact. Only the title reaches the context, capped at 200 characters.
- **The same calendar configured twice shows twice.** A user whose Google calendar is also
  configured as a CalDAV or iCal source sees its events from both. No cross-source deduplication.
- **One page per calendar.** A search reads up to 2,500 occurrences per calendar in its window
  (default 90 days) and does not paginate further.
- **Recurring events.** Search results name each occurrence, with the series id alongside; changing
  the series means passing the series id. A recurrence change on a single occurrence is rejected by
  Google and reported as such.
- **The context viewer page shows no Google events.** It renders context without an acting user.

## Verification

`tests/functional/tools/test_google_calendar_tools.py` covers calendar listing and visibility,
search across and within Google calendars, per-user isolation, write request bodies, the read-only
deployment, confirmation rendering, and the context filter and failure paths.
`tests/unit/services/test_oauth_integration_state.py` covers the extended taint floor.
