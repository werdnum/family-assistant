---
name: Skill Creation
description: How to create reusable skills (procedural instructions) using notes with YAML frontmatter.
---

# Skill Creation

Skills are special notes containing procedural instructions that the assistant can load on demand.
Unlike regular notes (which store facts), skills teach the assistant *how* to do something.

## Creating a Skill

Use the `add_or_update_note` tool with YAML frontmatter at the top of the content:

```
---
name: My Skill Name
description: One-line description shown in the skill catalog.
---

Detailed step-by-step instructions here...
```

**Required frontmatter fields:**

- `name` — Display name for the skill catalog
- `description` — One-line summary (this is all the assistant sees until it loads the full skill)

**Optional frontmatter fields:**

- `visibility_labels` — List of profile labels that can access this skill (omit for universal
  access)

## How Skills Work

1. The assistant sees a **catalog** of available skills (name + description) in every conversation
2. When a skill is relevant, it loads the full content on demand via `get_note`
3. This keeps conversations lightweight while making specialized knowledge available

## What Makes a Good Skill

- **Procedural, not factual** — Skills contain step-by-step instructions, not just information. Use
  regular notes for facts.
- **Self-contained** — Include all the context the assistant needs to follow the instructions
- **Descriptive name and description** — The assistant selects skills based on the catalog entry
  alone, so the name and description must clearly convey when the skill applies
- **Tool references** — Mention specific tool names and parameters so the assistant knows exactly
  what to call

## Example

```
---
name: Weekly Meal Planning
description: Guide for creating a weekly meal plan based on family preferences and calendar.
---

# Weekly Meal Planning

## Steps

1. Check the calendar for the upcoming week to identify busy evenings
2. Search notes for family food preferences and dietary restrictions
3. For busy evenings, suggest quick meals (30 min or less)
4. For free evenings, suggest more elaborate recipes
5. Ask the user to confirm or adjust the plan
6. Create a shopping list note with all ingredients
7. Set a reminder for the preferred grocery shopping day

## Tips

- Check for existing meal-related notes before starting
- Account for leftovers (cook extra on free nights for busy night lunches)
- Rotate proteins throughout the week for variety
```

## Visibility Labels

Restrict a skill to specific processing profiles:

```
---
name: Data Analysis Workflow
description: Step-by-step guide for analyzing datasets with charts.
visibility_labels:
  - data_visualization
---
```

This skill would only appear in the catalog for sessions using the `data_visualization` profile.

## Built-in vs User Skills

- **Built-in skills** ship with the assistant (browser automation, camera, image tools, scheduling)
- **User skills** (DB-based) are created via `add_or_update_note` with frontmatter
- User skills with the same name as a built-in skill override the built-in version
