# Notes and Skills

**What's here:** saving facts the assistant should remember, managing them, and teaching it reusable
instructions ("skills").

## Notes

Notes are the assistant's long-term memory for facts you give it. They persist across conversations
and are automatically indexed so you can search them later.

### Saving

- "Remember: the plumber's number is 555-1234."
- "Add a note titled 'Vacation Ideas' with the content 'Visit the Grand Canyon'."
- "Update the note 'Meeting Notes' with 'Discuss budget'."
- "Append to the note 'Meeting Notes': 'Also discuss timeline'." — adds to the note instead of
  replacing its content.

### Retrieving

- "What was the Wi-Fi password?"
- "Get the note titled 'Wi-Fi Password'."
- "List all my notes."
- "Search my notes for 'project deadline'."

Search is semantic, so you don't need the exact wording you originally used.

### Removing

- "Delete the note 'Old Shopping List'."

### Managing notes in the web interface

The **Notes** page lists everything, and lets you edit content, delete notes, search, and control
whether a note is included in the assistant's context automatically. Notes are also fully editable
in the iOS app.

### What's in the assistant's context

Most notes aren't loaded into every conversation — that would make the assistant's prompt grow
without limit. Instead, the assistant sees the *titles* of your notes and loads the full content on
demand when a note is relevant.

A small number of notes can be marked to be included every time. That's worth doing for short,
evergreen context — durable preferences, household policies, persistent facts — and not for one-off
details or long lists.

### Attachments on notes

Notes can carry files and images. See [attachments.md](attachments.md).

## Skills

Skills are notes that hold *procedural* instructions rather than facts: step-by-step guidance the
assistant follows when a particular kind of task comes up.

The assistant sees a catalog of available skills — name plus a short description — in every
conversation, and loads a skill's full instructions only when it's relevant. That keeps
conversations light while making specialised knowledge available.

### Built-in skills

Several skills ship with the assistant, covering browser automation, camera integration, image
tools, scheduling, and shopping. The instructions themselves are built in and need no setup — but a
skill only helps if the thing it drives is available. The camera skill needs cameras configured, and
the shopping skill needs checkout signing keys set up before it can hand you a checkout link.

### Writing your own

Ask the assistant to save a note whose content starts with YAML frontmatter containing `name` and
`description`:

```
Remember this as a note titled "Cooking Helper":
---
name: Cooking Helper
description: Guides meal planning and recipe adaptation for the family's dietary preferences.
---
When asked about meal planning:
1. Check the family's dietary preferences note
2. Consider the weekly schedule for time constraints
3. Suggest recipes that match both preferences and available time
```

A note with that frontmatter joins the skill catalog the assistant consults. It also stays in your
ordinary notes list, so you edit or delete it there like any other note.

Advanced users can also place `.md` files with the same frontmatter in a directory configured by the
operator. Those load at startup, and a saved skill with the same name overrides the file.

### Visibility

Skills support the same visibility labels as notes, but the labels are not part of the frontmatter —
only `name` and `description` are read from there. To restrict a skill, ask for the restriction when
you create it ("save this as a skill, visible only to the research profile"), so the assistant sets
the labels on the note itself. A skill saved without asking gets the default labels for the profile
that created it.

## Troubleshooting

- **The assistant didn't recall something you told it.** Check that it actually saved a note — ask
  "list all my notes". Saying "remember X" should produce a note; if the assistant only said it
  would remember, ask it to save the note explicitly.
- **A note isn't being used in conversation.** Titles are always visible to the assistant, but full
  content is loaded on demand. Mentioning the note by title ("check my Wi-Fi Password note") makes
  it load reliably.
