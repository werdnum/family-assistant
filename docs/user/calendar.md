# Calendar and Events

**What's here:** asking about your schedule, adding, changing, or deleting events on connected
calendars, and using your own Google Calendar.

Three kinds of calendar can be available, and they differ in what you can do:

- **A shared account your operator connects** (a family calendar on iCloud, Nextcloud, or similar
  over CalDAV) — the assistant can read the schedule *and* add, change, and delete events.
- **A subscribed feed your operator connects** (an iCal/`.ics` URL — a school calendar, a sports
  fixture list) — read-only. The assistant sees those events when you ask what's on, but cannot add
  to or edit them.
- **Your own Google Calendar**, once you connect your Google account — see
  [below](#your-own-google-calendar). Only you see it; other household members see their own.

So if the assistant can tell you about your week but refuses to create an event, the likely reason
is that only read-only feeds are connected. Ask your operator.

For reminders and recurring tasks — which are separate from calendar events — see
[scheduling.md](scheduling.md).

## Discovering your calendars

- "What calendars do you have access to?"
- "List all connected calendars."

The assistant will list each available calendar, its name and identifier, whether it is CalDAV or an
iCal feed, whether it is writable or read-only, and which calendar is the default for new events.

## Asking about your schedule

- "What's happening tomorrow?"
- "Do we have anything scheduled next Saturday?"
- "List events for the next 14 days."
- "Are there any events next Tuesday?"
- "What flights or trips are on my TripIt calendar next month?"

When the assistant shows your schedule, each event indicates which calendar it belongs to in
brackets (e.g., `[Family]`, `[TripIt Trips]`, `[School]`). Searches query across all connected
CalDAV calendars and iCal subscription feeds at once.

An invitation or subscribed event can contain text written by someone outside your household. The
assistant can tell you what it says, but may ask for confirmation before taking a sensitive action
based on that text. Events you created yourself and unchanged events the assistant added are handled
according to where their content came from.

## Adding events

- "Add dentist appointment for June 5th at 10 AM."
- "Schedule 'Team Lunch' tomorrow from 12 PM to 1 PM."
- "Add 'Soccer practice' to the Kids calendar on Saturday at 10am."

If you don't specify a calendar, the event goes to your default primary calendar. If you want it on
a specific calendar, mention the calendar name. If you attempt to add an event to a read-only feed
(such as a school schedule or TripIt subscription), the assistant will let you know it is read-only
and suggest using a writable calendar instead.

The assistant checks for similar events across all your calendars (including read-only subscription
feeds) at nearby times before creating one, so it may ask whether a new event is really a duplicate.
If it isn't, tell it to go ahead.

## Changing and deleting events

The assistant finds the event first, then asks you to approve the change:

- "Change the 'Team Lunch' to 12:30 PM."
- "Delete the 'Dentist Appointment' on June 5th."

You'll see a confirmation with the details — inline buttons in Telegram, a dialog in the web
interface, an actionable notification on iOS. See
[confirmations-and-safety.md](confirmations-and-safety.md).

If several events could match, the assistant asks which one you mean. Naming the date narrows it
down quickly.

## Your own Google Calendar

If your operator has enabled Google, connect your account under **Settings → Connected Accounts**
(the steps are in [google-workspace.md](google-workspace.md)). If you connected before calendar
access was available, click **Reconnect** and approve the calendar permissions.

Once connected:

- **Your primary Google calendar is always in view.** When you ask "what's on today?", the assistant
  already knows your upcoming events on it, alongside the family calendars.
- **Your other Google calendars** (a kids' calendar, a work calendar, calendars shared with you) are
  searched when you ask about your schedule, but aren't shown up front. Calendars you've hidden in
  Google Calendar are only searched when you name them: "check my Holidays calendar for next month."
- **You can add, change, and delete events** on any Google calendar you can edit: "add swimming
  lessons to my Google calendar on Thursday at 4pm", "move my dentist appointment to 11". The family
  calendar is still the default for new events, so say "my Google calendar" when that's where you
  want it (if no family calendar is set up, your Google calendar is the default). Changes and
  deletions ask for your approval first.
- **Nobody gets emailed.** The assistant never invites guests, and when it changes or deletes an
  event that has guests it doesn't send them updates. Use Google Calendar itself when you want to
  invite people or notify them.

**Invitations you haven't answered** don't appear in the up-front view, and neither do events Google
creates automatically from your email (flight or restaurant bookings). Anyone can send you an
invitation, so the assistant only shows events you created or accepted without being asked. They
still turn up when you ask it to search your calendar.

Each person's Google Calendar is private to them: when you ask in a shared chat, the answer is
visible to everyone in that chat, but the assistant can never read another household member's Google
Calendar for you.

## Troubleshooting

- **Your Google events are missing.** Check **Settings → Connected Accounts**. If it says calendar
  permissions are missing, click **Reconnect** and approve them. If your Google connection needs
  re-authorizing, the assistant tells you so when it can't load your calendar.
- **The event wasn't created.** Be specific about the date and time — "next Tuesday at 2pm" works
  better than "sometime next week". If the assistant can read your schedule but can't write to it,
  your deployment may only have read-only feeds connected; ask your operator.
- **The assistant flagged a duplicate.** Review the events it listed. If yours is genuinely
  different (a different doctor, a different purpose), tell it to create the event anyway.
- **A change is waiting.** Modifications and deletions don't take effect until you approve the
  confirmation, and confirmations expire if you leave them too long — see
  [confirmations-and-safety.md](confirmations-and-safety.md).
