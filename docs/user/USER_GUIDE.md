# Family Assistant User Guide

Family Assistant is a helper for your household: it keeps track of notes, schedules, documents, and
home devices, and you talk to it in plain language through Telegram, the web interface, or the iOS
app.

This page is an **index**. Each topic lives in its own guide, so you (or the assistant) can read
just the part you need.

## Start here

| Guide                                  | Read it when you want to…                                       |
| -------------------------------------- | --------------------------------------------------------------- |
| [QUICK_START.md](QUICK_START.md)       | Get going in five minutes with a handful of example requests    |
| [interfaces.md](interfaces.md)         | Know what Telegram, the web app, and the iOS app each offer     |
| [slash-commands.md](slash-commands.md) | Look up `/browse`, `/research`, `/engineer` and the other modes |

## Everyday features

| Guide                                                | Covers                                                                   |
| ---------------------------------------------------- | ------------------------------------------------------------------------ |
| [notes-and-skills.md](notes-and-skills.md)           | Saving facts as notes, and teaching the assistant reusable skills        |
| [calendar.md](calendar.md)                           | Adding, finding, changing, and deleting calendar events                  |
| [scheduling.md](scheduling.md)                       | Reminders, follow-ups, one-off callbacks, and recurring schedules        |
| [documents-and-search.md](documents-and-search.md)   | Indexing files and web pages, and searching everything you've stored     |
| [attachments.md](attachments.md)                     | Sending photos and files, and moving them between tools                  |
| [email.md](email.md)                                 | Emailing or forwarding mail to the assistant, and what it may do with it |
| [google-workspace.md](google-workspace.md)           | Connecting your Google account for Gmail and Drive access                |
| [smart-home.md](smart-home.md)                       | Controlling Home Assistant devices, and who's home                       |
| [automations.md](automations.md)                     | Automations that react to events or run on a schedule                    |
| [research-and-browsing.md](research-and-browsing.md) | Web search, page summaries, browsing sites, and deep research            |
| [media.md](media.md)                                 | Analysing photos, and generating or editing images and video             |
| [shopping.md](shopping.md)                           | Finding products online and getting a checkout link                      |

## Deeper dives

| Guide                                                      | Covers                                                                    |
| ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| [confirmations-and-safety.md](confirmations-and-safety.md) | Why the assistant asks for approval, and how it handles untrusted content |
| [troubleshooting.md](troubleshooting.md)                   | What to do when something doesn't work, and how to report a bug           |
| [scripting.md](scripting.md)                               | Writing automation scripts                                                |
| [browser_automation.md](browser_automation.md)             | `/browse` and `/browse_visual` in detail                                  |
| [camera_integration.md](camera_integration.md)             | Live camera feeds and reviewing footage                                   |
| [image_tools.md](image_tools.md)                           | Image generation, editing, and annotation in detail                       |
| [data_visualization.md](data_visualization.md)             | Building charts, plus [vega_lite_reference.md](vega_lite_reference.md)    |

## What the assistant knows

The assistant draws on:

- **What you tell it** — notes you ask it to remember.
- **Connected calendars** — shared family calendars linked by your operator.
- **The current conversation** — recent messages, for context.
- **Stored documents** — notes, uploaded files, indexed web pages, and forwarded email.
- **Your Google account** — Gmail and Drive, if you have connected one.
- **Smart home state** — device states, sensor readings, and who is home, via Home Assistant.

Not every feature is switched on in every deployment. Calendars, Home Assistant, cameras, email
intake, and Google access each need setup by whoever runs your Family Assistant; if something in
these guides doesn't seem to exist, ask them.

______________________________________________________________________

Need help? Ask the assistant "what can you help me with?", or contact the person who set up Family
Assistant for your household.
