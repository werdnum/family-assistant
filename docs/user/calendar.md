# Calendar and Events

**What's here:** asking about your schedule and adding, changing, or deleting events on connected
calendars.

Calendar access requires a CalDAV connection (Google Calendar, iCloud, and similar) configured by
your operator. For reminders and recurring tasks — which are separate from calendar events — see
[scheduling.md](scheduling.md).

## Asking about your schedule

- "What's happening tomorrow?"
- "Do we have anything scheduled next Saturday?"
- "List events for the next 14 days."
- "Are there any events next Tuesday?"

## Adding events

- "Add dentist appointment for June 5th at 10 AM."
- "Schedule 'Team Lunch' tomorrow from 12 PM to 1 PM."
- "Add 'Pick up groceries' on Saturday at 10am."

The assistant checks for similar events at nearby times before creating one, so it may ask whether a
new event is really a duplicate. If it isn't, tell it to go ahead.

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
  better than "sometime next week". If the assistant reports it can't reach a calendar, ask your
  operator to check the CalDAV connection.
- **The assistant flagged a duplicate.** Review the events it listed. If yours is genuinely
  different (a different doctor, a different purpose), tell it to create the event anyway.
- **A change is waiting.** Modifications and deletions don't take effect until you approve the
  confirmation. Approvals stay open for 24 hours.
