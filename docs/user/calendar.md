# Calendar and Events

**What's here:** asking about your schedule and adding, changing, or deleting events on connected
calendars.

Your operator connects the calendars. Two kinds can be connected, and they differ in what you can
do:

- **A full account** (Google Calendar, iCloud, and similar over CalDAV) — the assistant can read
  your schedule *and* add, change, and delete events.
- **A subscribed feed** (an iCal/`.ics` URL — a school calendar, a sports fixture list) — read-only.
  The assistant sees those events when you ask what's on, but cannot add to or edit them.

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

## Troubleshooting

- **The event wasn't created.** Be specific about the date and time — "next Tuesday at 2pm" works
  better than "sometime next week". If the assistant can read your schedule but can't write to it,
  your deployment may only have read-only feeds connected; ask your operator.
- **The assistant flagged a duplicate.** Review the events it listed. If yours is genuinely
  different (a different doctor, a different purpose), tell it to create the event anyway.
- **A change is waiting.** Modifications and deletions don't take effect until you approve the
  confirmation, and confirmations expire if you leave them too long — see
  [confirmations-and-safety.md](confirmations-and-safety.md).
