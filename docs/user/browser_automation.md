# Browser Automation Guide

This guide explains how to use the Family Assistant's browser automation features to interact with
websites, fill out forms, and perform complex web tasks.

## Overview

Browser automation allows the assistant to control a web browser on your behalf. This is useful when
you need to:

- Navigate websites that require interaction (clicking, scrolling, typing)
- Fill out forms or complete web-based tasks
- Extract information from dynamic or JavaScript-heavy pages
- Perform multi-step web workflows

The assistant uses a specialized browser profile powered by an AI model designed for computer use,
allowing it to see and interact with web pages like a human would.

## When to Use Browser Automation

### Good Use Cases

- **Complex web forms** - Multi-step forms, login flows, or interactive applications
- **JavaScript-heavy sites** - Pages that load content dynamically
- **Multi-step workflows** - Tasks requiring navigation through multiple pages
- **Interactive research** - When you need to click through results, expand sections, or interact
  with page elements

### When NOT to Use Browser Automation

- **Simple page content** - For reading articles or static pages, ask directly: "What does this page
  say: https://example.com/article"
- **Basic web searches** - The assistant can search the web without browser automation
- **Saving pages for later** - Use "Save this page for later: [URL]" instead

## How to Use Browser Automation

### The `/browse` Command

Prefix your request with `/browse` to activate browser automation mode:

```
/browse Go to example.com and find the contact form
/browse Search for recent reviews of the XZ-100 camera
/browse Navigate to the settings page and check my account status
```

### Natural Language Examples

Once in browse mode, you can give natural instructions:

```
/browse Go to amazon.com and search for wireless headphones under $50
```

```
/browse Visit the city library website and check if "The Great Gatsby" is available
```

```
/browse Go to weather.com and tell me the 7-day forecast for Seattle
```

```
/browse Navigate to my bank's website login page and take a screenshot
```

## Available Browser Actions

The assistant can perform these actions while browsing:

### Navigation

- **Open browser** - Starts with a search page (Google)
- **Navigate to URL** - Goes directly to a specific web address
- **Go back/forward** - Navigate through browser history
- **Search** - Return to the search page

### Interaction

- **Click** - Click on buttons, links, and other elements
- **Type text** - Enter text into input fields and forms
- **Scroll** - Scroll up, down, left, or right on the page
- **Hover** - Move the mouse over elements to reveal tooltips or menus
- **Drag and drop** - Move elements on interactive pages

### Keyboard

- **Key combinations** - Press keyboard shortcuts (e.g., Ctrl+C, Enter)
- **Form submission** - Press Enter to submit forms

### Timing

- **Wait** - Pause for pages to load or animations to complete

## Examples by Use Case

### Online Shopping

```
/browse Go to bestbuy.com and find the price of a 65-inch Samsung TV
```

The assistant will navigate to the site, search for the product, and report the price.

### Information Research

```
/browse Check the opening hours for the Metropolitan Museum of Art
```

The assistant will find the museum's website and locate the hours information.

### Form Completion

```
/browse Go to the DMV appointment scheduler and show me available dates next week
```

The assistant will navigate the site and gather the available appointment information.

### Account Checking

```
/browse Go to my utility provider's website and find where to view my billing history
```

The assistant will navigate the site and identify the relevant section (though it cannot log in
without credentials).

## Understanding Screenshots

When using browser automation, the assistant takes screenshots to "see" what's on the page. After
each action, it captures the current state to understand what happened and decide what to do next.

The assistant may share screenshots with you to:

- Show what it found
- Ask for clarification when a page is unclear
- Confirm it completed the task

## Limitations

### Cannot Do

- **Access your logged-in accounts** - The browser session is separate from your personal browser,
  so the assistant cannot see your saved passwords or active sessions
- **Download files to your computer** - Files downloaded go to the assistant's environment, not your
  device
- **Interact with desktop applications** - Only web pages in the browser are accessible
- **Bypass CAPTCHAs** - The assistant cannot solve CAPTCHA challenges
- **Access content behind paywalls** - Unless the content is publicly available

### May Have Difficulty With

- **Rapidly changing pages** - Content that updates frequently or uses heavy animations
- **Complex multi-factor authentication** - While it can navigate login pages, MFA requirements may
  block access
- **Sites with anti-bot protections** - Some websites detect and block automated browsers
- **Very slow-loading pages** - There are timeout limits on page loads

## Privacy and Security Considerations

### What the Assistant Sees

- The assistant can see everything displayed on web pages during automation
- Screenshots are taken after each action
- Page content, including any visible personal information, is processed by the AI model

### Session Isolation

- Browser sessions are isolated per conversation
- No cookies, passwords, or session data persist between conversations
- Your personal browser is not affected

### Sensitive Information

- **Never share passwords** in your browser automation requests
- **Avoid navigating to pages with sensitive data** unless necessary
- **Be cautious with banking and financial sites** - the assistant can see displayed information

### Best Practices

- Use browser automation for public information and navigation
- Avoid requesting the assistant to log into accounts with sensitive data
- Review what information is displayed before asking the assistant to screenshot or describe a page

## Tips for Best Results

1. **Be specific about your goal** - "Find the contact email on the About page" is better than "Find
   contact info"

2. **Provide the full URL when possible** - Starting with the right page saves time

3. **Break complex tasks into steps** - For multi-page workflows, guide the assistant through each
   step

4. **Describe what you're looking for** - "Look for a blue 'Submit' button at the bottom of the
   form"

5. **Be patient with slow pages** - Some sites take time to load; the assistant will wait as needed

## Troubleshooting

### Page Won't Load

- Try providing the full URL with `https://`
- Some sites block automated browsers; try a different approach or site

### Can't Find an Element

- Describe what you're looking for more specifically
- Ask the assistant to scroll down or look in a different section
- Some elements may only appear after certain actions (hovering, clicking)

### Action Didn't Work

- Dynamic pages may require waiting for content to load
- Ask the assistant to try the action again
- Some interactive elements may not work with automation

### Session Timeout

- Long-running browser sessions may time out
- For complex tasks, break them into smaller requests
- Start a new `/browse` session if needed

## Related Features

- **[User Guide](USER_GUIDE.md)** - Full assistant documentation
- **[Scripting Guide](scripting.md)** - Automate tasks with scripts
- **Document Ingestion** - Save web content: "Save this page for later: [URL]"
