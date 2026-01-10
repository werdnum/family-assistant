# Frequently Asked Questions (FAQ)

This document answers common questions about using Family Assistant. For detailed information, see
the [User Guide](USER_GUIDE.md) and other specialized guides.

______________________________________________________________________

## Getting Started

### How do I access Family Assistant?

You can interact with Family Assistant through two main interfaces:

- **Telegram** (Primary) - Find the bot in your Telegram app and start chatting. The bot name will
  be provided by your administrator.
- **Web Interface** - Access the web app at the URL provided by your administrator. The interface
  features a modern design with dark mode support and mobile optimization.

### What can Family Assistant help me with?

Family Assistant can help you with:

- **Notes and Information** - Store and retrieve information like passwords, phone numbers, and
  lists
- **Calendar Management** - Add, view, modify, and delete calendar events
- **Reminders and Tasks** - Set one-time or recurring reminders
- **Document Search** - Search through notes, emails, PDFs, and indexed web pages
- **Smart Home Control** - Control lights, thermostats, and other Home Assistant devices (if
  configured)
- **Automation** - Create event-triggered or scheduled automations
- **Image and Video Generation** - Create AI-generated images and videos
- **Web Browsing** - Navigate websites and fill forms using the `/browse` command
- **Research** - Conduct in-depth research on topics using the `/research` command
- **Data Visualization** - Create charts and graphs from your data

### How do I get started?

Just start typing naturally, like texting a friend. Try these examples:

- "Remember: The Wi-Fi password is BlueOcean2024"
- "What's on my calendar tomorrow?"
- "Remind me to call Mom at 5pm"
- "Add dentist appointment for next Tuesday at 2pm"

See the [Quick Start Guide](QUICK_START.md) for more examples.

______________________________________________________________________

## Notes and Documents

### How do I create a note?

Use natural language to create notes:

- "Remember: The plumber's number is 555-1234"
- "Add a note titled 'Shopping List' with milk, eggs, and bread"

Notes are stored permanently and can be retrieved later.

### How do I search my notes?

Ask the assistant to search:

- "Search my notes for 'project deadline'"
- "What was the plumber's number?"
- "Find notes about vacation"

The assistant uses semantic search to find relevant content even if you don't remember the exact
wording.

### How do I update a note?

You can update notes in several ways:

- "Update the note 'Shopping List' with new items: apples, oranges"
- "Append to the note 'Meeting Notes' with 'Discuss budget'"

You can also edit notes directly through the Web Interface's Notes page.

### How do I delete a note?

Simply ask:

- "Delete the note 'Old Shopping List'"
- "Remove the note about the old password"

______________________________________________________________________

## Calendar and Events

### How do I add a calendar event?

Use natural language to describe your event:

- "Add dentist appointment for next Tuesday at 2pm"
- "Schedule team lunch tomorrow from 12 to 1pm"
- "Add 'Pick up groceries' on Saturday at 10am"

The assistant will add the event to your connected calendar.

### Why didn't my event get created?

Common reasons for event creation issues:

- **Ambiguous time** - Be specific with dates and times. "Next Tuesday at 2pm" works better than
  "sometime next week"
- **Calendar not connected** - Ask your administrator to verify the CalDAV calendar connection
- **Duplicate detection** - The assistant may ask for confirmation if a similar event already exists

If an event modification requires confirmation, you'll see approve/deny buttons (in Telegram) or a
dialog box (in the Web Interface).

### How do I see my upcoming events?

Ask about your schedule:

- "What's on my calendar tomorrow?"
- "Show me events this week"
- "What's happening next Saturday?"
- "List events for the next 14 days"

______________________________________________________________________

## Reminders and Tasks

### How do I set a reminder?

Set reminders using natural language:

- "Remind me to call the dentist at 3pm"
- "Set a reminder for tomorrow at 9am to review the report"
- "Don't let me forget about the meeting at 4:30" (This creates a reminder with follow-ups if you
  don't respond)
- "Remind me in 2 hours to check the oven"

### Why didn't my reminder fire?

Common reasons for missed reminders:

- **Timezone issues** - Ensure times are specified clearly. The assistant interprets times in your
  local timezone.
- **Task not scheduled** - Check "Show me my pending callbacks" to verify the reminder exists
- **Network issues** - If Telegram notifications are delayed, check your network connection

To view all scheduled tasks, ask "Show me my pending callbacks" or check the Tasks page in the Web
Interface.

### How do I schedule recurring tasks?

Create recurring tasks with natural language:

- "Send me a weather update every morning at 7am"
- "Remind me to take medication every day at 8am and 8pm"
- "Run a cleanup script every Sunday at midnight"

The assistant uses RRULE format internally to handle complex schedules (daily, weekly, monthly,
etc.). See the [Scheduling Guide](scheduling.md) for details.

### How do I cancel a scheduled task?

Ask the assistant to cancel:

- "Cancel the reminder about the meeting"
- "Stop all daily weather updates"
- "Delete all callbacks related to project X"

For recurring tasks, each future instance appears separately and can be cancelled individually.

______________________________________________________________________

## Automation

### How do I create an automation?

You can create automations in two ways:

**Via Telegram/Web Chat:**

- "Alert me when the garage door opens after 10pm"
- "Notify me when Alex arrives home"
- "Watch for any new documents about invoices"

**Via Web Interface:** Navigate to the Automations section and click "Create New Automation" to use
the visual form.

Use the `/automate` slash command for the automation-focused profile.

### Why isn't my automation running?

If your event automation isn't triggering:

1. **Check event matching** - Use "Show me recent events from [source]" to see what events are being
   captured
2. **Test your conditions** - Ask "Test if [condition] would match recent events"
3. **Verify field names** - Make sure you're using exact field names from the event data (use dot
   notation for nested fields like "new_state.state")
4. **Check automation status** - Verify the automation is enabled in the Automations page
5. **Review condition scripts** - For complex conditions, test them first: "Test this condition
   script with a sample event: [your script]"

Common issues:

- Home Assistant sends state_changed events even when only attributes change - use condition scripts
  to detect actual state transitions
- Condition scripts must return a boolean value

### How do I debug my script?

Before scheduling a script, test it:

- "Validate this script syntax: [paste your script]"
- "Test this event script with a sample temperature event: [paste your script]"

Key debugging tips:

- Scripts have a 10-minute timeout
- Starlark has no try-except, so check inputs carefully
- Most tools return JSON strings - use `json_decode()` to parse them
- Check the Tasks page in the web UI for script execution logs and error messages
- View script execution history in the Automations page

See the [Scripting Guide](scripting.md) for complete scripting documentation.

______________________________________________________________________

## Smart Home

### How do I control my devices?

Use natural language with exact device names from your Home Assistant setup:

- "Turn on the kitchen lights"
- "Is the garage door closed?"
- "Set the thermostat to 70 degrees"
- "What's the temperature in the baby's room?"

### Why can't I see my devices?

If device control isn't working:

1. **Home Assistant not configured** - Ask your administrator to verify the Home Assistant
   integration
2. **Wrong device names** - Use the exact entity names from Home Assistant (e.g., "Living Room
   Lamp", "Downstairs Thermostat")
3. **Connection issues** - The assistant automatically reconnects if the connection is lost. You may
   see brief interruptions during reconnection.

Ask "What lights are available?" or similar to see available entities.

______________________________________________________________________

## Technical Issues

### Why isn't the assistant responding?

If the assistant seems unresponsive:

1. **Check your connection** - Ensure you have internet access
2. **Try a simple message** - Send "Hello" to test basic communication
3. **Wait a moment** - Complex requests may take longer to process
4. **Check Telegram** - Ensure notifications are enabled for the bot
5. **Try the Web Interface** - If Telegram isn't working, try the web interface as an alternative

### Why is the assistant giving unexpected responses?

Tips for better results:

- **Be specific** - "Remind me to pick up dry cleaning at 5pm tomorrow" works better than "remind me
  about dry cleaning"
- **Use full URLs** - When asking about web content, include the complete address starting with
  `https://`
- **Rephrase your request** - Sometimes slightly different wording makes a big difference
- **Correct mistakes** - You can tell the assistant "Actually, the appointment is at 3 PM" to
  correct information

### How do I reset my conversation?

Each conversation maintains its own context. To start fresh:

- **Telegram** - Start a new conversation or wait for the context to naturally reset
- **Web Interface** - Create a new conversation from the chat interface

### How do I report a bug?

If you encounter an issue:

1. Note what you were trying to do
2. Note the exact error message or unexpected behavior
3. Contact the person who set up your Family Assistant
4. For technical issues, check the Tasks page in the Web Interface for error logs

______________________________________________________________________

## Browser Automation

### Why won't the page load?

If browser automation has trouble with a page:

- **Provide the full URL** - Include `https://` at the beginning
- **Site blocks automation** - Some sites detect and block automated browsers
- **Try a different approach** - For simple page content, ask directly without `/browse`

### Why can't I log into my account?

Browser automation limitations:

- **Separate session** - The browser session is isolated and doesn't have your saved passwords or
  active sessions
- **CAPTCHAs** - The assistant cannot solve CAPTCHA challenges
- **Multi-factor authentication** - MFA requirements may block automated access

See the [Browser Automation Guide](browser_automation.md) for more details.

______________________________________________________________________

## Camera Integration

### Why can't I see my cameras?

If camera access isn't working:

1. **Home Assistant cameras** - Verify the camera entity exists in Home Assistant (entities starting
   with `camera.`)
2. **Reolink cameras** - Check network connectivity and verify credentials are correct
3. **Ask for available cameras** - "What cameras do I have?" to see what's configured

### Why are there no recordings for a time period?

If recordings are missing:

1. Check if the camera was online during that period
2. Verify recording settings on the camera/NVR
3. Check if the time range is within the camera's retention period
4. Use "Get recordings from [camera]" to identify gaps

See the [Camera Integration Guide](camera_integration.md) for more details.

______________________________________________________________________

## Image and Video Generation

### Why did image generation fail?

If image generation fails:

- **Rephrase your description** - Be more specific about what you want
- **Check content guidelines** - Some content may not be allowed
- **Try a different style** - Specify "photorealistic" or "artistic" in your request

### Why doesn't my image transformation work as expected?

For better transformations:

- **Provide specific instructions** - Describe exactly what you want changed
- **Break complex edits into steps** - Try simpler transformations first
- **Consider image quality** - Some transformations work better on certain types of images

See the [Image Tools Guide](image_tools.md) for more details.

______________________________________________________________________

## Data Visualization

### Why is my chart blank?

Common causes:

- **Dataset naming** - When using the `data` parameter, your spec must reference the dataset as
  `{"name": "data"}` (the default name)
- **Invalid data** - Sensor data may contain non-numeric values like "unavailable" or "unknown" that
  need to be filtered
- **Wrong field names** - Check that referenced fields exist in your dataset

### How do I clean sensor data?

Use jq to filter invalid values:

```
jq_query(attachment_id, '.[] | select(.state != null and .state != "unavailable")')
```

See the [Data Visualization Guide](data_visualization.md) for complete examples.

______________________________________________________________________

## Additional Resources

- [Quick Start Guide](QUICK_START.md) - Get started in 5 minutes
- [User Guide](USER_GUIDE.md) - Complete feature documentation
- [Feature Overview](FEATURES.md) - Comprehensive catalog of all features
- [Scheduling Guide](scheduling.md) - Reminders and recurring tasks
- [Scripting Guide](scripting.md) - Advanced automation with scripts
- [Browser Automation Guide](browser_automation.md) - Complex web interactions
- [Camera Integration Guide](camera_integration.md) - Security camera features
- [Image Tools Guide](image_tools.md) - Image generation and manipulation
- [Data Visualization Guide](data_visualization.md) - Charts and graphs

______________________________________________________________________

**Need more help?** Ask the assistant "What can you help me with?" or contact the person who set up
your Family Assistant.
