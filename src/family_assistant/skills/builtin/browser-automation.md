---
name: Browser Automation
description: Guide for using the /browse command to navigate websites, fill forms, and perform multi-step web workflows.
---

# Browser Automation

## When to Use

- **Complex web forms** - Multi-step forms, login flows, interactive applications
- **JavaScript-heavy sites** - Pages that load content dynamically
- **Multi-step workflows** - Tasks requiring navigation through multiple pages
- **Interactive research** - Clicking through results, expanding sections

**Don't use for**: Simple page reading (ask directly), basic web searches, saving pages.

## Activation

Prefix requests with `/browse`:

```
/browse Go to example.com and find the contact form
/browse Search for recent reviews of the XZ-100 camera
```

## Available Actions

- **Navigate**: Open browser, go to URL, back/forward, search
- **Interact**: Click, type, scroll, hover, drag and drop
- **Keyboard**: Key combinations, form submission
- **Wait**: Pause for page loads or animations

## Limitations

- Cannot access user's logged-in accounts (isolated session)
- Cannot download files to user's device
- Cannot bypass CAPTCHAs or paywalls
- May struggle with anti-bot protections or rapidly changing pages

## Tips

1. Be specific about the goal
2. Provide full URLs when possible
3. Break complex tasks into steps
4. Describe what you're looking for clearly
