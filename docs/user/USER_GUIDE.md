# Your Family Assistant - User Guide

Welcome to your personal family assistant! This guide explains how to interact with the assistant and what it can do to help manage your family's information and schedule.

## 1. Introduction: Meet Your Family Assistant

**What is it?**Think of this as a central helper designed to keep track of shared family information, schedules, notes, and even interact with some of your home devices.

**What's the goal?**To simplify managing the details of family life by having one place to ask questions, store information, and get updates.

**How does it work?**You interact with the assistant primarily by chatting with it on Telegram, just like talking to a person. It understands your requests and uses its knowledge and connected services to respond or perform actions.

## 2. Getting Started: How to Talk to the Assistant

* **Telegram (Primary):**
    *Find the bot contact in your Telegram app (the person who set it up will tell you its name).
    *Start a chat and simply send messages with your questions or requests.
    *You can also use **slash commands**to activate specialized modes for certain tasks. For example, typing `/browse` before your query activates a powerful web browsing mode for complex web tasks or research, while `/research` focuses the assistant on in-depth research questions.

* **Web Interface (Secondary):**
    *There's also a web page you can use for certain tasks:
        *Managing notes.
        *Viewing message history and background tasks.
        *Uploading documents for the assistant to learn from.
        *Managing API tokens for programmatic access.
        *Searching indexed documents.
    *Access it here: `{{ SERVER_URL }}` (This link will be replaced with the actual URL).

* **Email (Future):**
    *Soon, you might be able to forward emails (like flight confirmations or event invitations) to a special address so the assistant can automatically store the information. Stay tuned!

## 3. What Can the Assistant Do For You? (Core Features)

You can ask the assistant a wide variety of things:

* **Answer Your Questions:**
    * **About upcoming events:**"What's happening tomorrow?", "Do we have anything scheduled next Saturday?", "List events for the next 14 days." (The assistant reads from connected family calendars.)
    * **About things you've told it (Notes):**"What was the Wi-Fi password?", "Remind me about the gift ideas we saved.", "Where did we put the spare keys?" (It uses the notes you've asked it to remember.)
    * **Add/Modify/Delete Calendar Events:**You can manage your calendar directly:
        * **Add:**"Add dentist appointment for June 5th at 10 AM." or "Schedule 'Team Lunch' tomorrow from 12 PM to 1 PM."
        * **Search:**"Are there any events next Tuesday?", "Find the dentist appointment in June." (This helps find events before modifying/deleting).
        * **Modify:**"Change the 'Team Lunch' to 12:30 PM." (Requires finding the event first. You will be asked to confirm the change.)
        * **Delete:**"Delete the 'Dentist Appointment' on June 5th." (Requires finding the event first. You will be asked to confirm the deletion.)
    * **About the current time/date:**"What time is it?", "What day is it today?" (Uses its built-in time service.)
    * **About the weather:**"What's the weather like today?", "Will it rain tomorrow in London?"
    * **About locations or directions:**"Find coffee shops near me.", "How do I get to the Eiffel Tower?" (If enabled, uses Google Maps.)
    * **About web content:**"Can you summarize this article: `[Full URL]`?", "What's the main point of this webpage: `[Full URL]`?" (For simple page summaries, the assistant can often fetch the content directly. If it has trouble or if the page is very complex (e.g., requires logins or interactions), you can try prefacing your request with `/browse`. Always provide the complete web address starting with `http://` or `https://`.)
    * **Search Your Documents:**"Search my notes for 'plumber number'.", "Find emails about the flight booking.", "Look for PDF documents related to 'insurance policy'." (The assistant can search through notes, indexed emails, PDFs, and other documents it has access to. Search results will be grouped by document, showing relevant snippets.)
    * **Manage Your Notes:**The notes system has been enhanced with powerful new features:
        * **Get specific notes:**"Get the note titled 'Wi-Fi Password'", "Show me the grocery list note"
        * **List all notes:**"List all my notes", "Show me all notes"
        * **Delete notes:**"Delete the note 'Old Shopping List'"
        * **Automatic indexing:**All notes are now automatically indexed for better search capabilities
        * **Smart inclusion:**Notes can be marked to automatically include in conversations when relevant
    * **Retrieve Full Documents:**After a search, if the assistant finds a document (e.g., "Document ID 123: Insurance Policy Scan"), you can ask: "Show me the full content of document 123." You can also click on search results in the Web UI to see a detailed view of the document. The document detail view now shows the complete text content at the top of the page, making it easy to read the full document without having to reconstruct it from search snippets. Large documents (like PDFs or long web pages) are fully accessible even if they were too large to process for search indexing.
    * **General knowledge & web searches:**"Search the web for reviews of the new park.", "Who won the game last night?", "Find me a recipe for banana bread." (The assistant can search the web for information using its default search capabilities. For more complex web research that might involve navigating multiple pages or interacting with sites, you can use the `/browse` command followed by your research query, e.g., `/browse find recent reviews for the XZ-100 camera and compare its features to the YZ-200`. Uses the Brave Search service for some searches.)
    * **Run Automation Scripts:**You can ask the assistant to execute scripts for complex automation:
        *"Execute a script that finds all TODO notes and creates a summary"
        *"Run a script to create prep notes for tomorrow's meetings"
        *"Write and execute a script that searches for project updates and emails me a digest"
        *See the [Scripting Guide](scripting.md) for more details on what scripts can do.

* **Remember Things (Notes):**
    *Tell it to save information permanently:
        *"Remember: The plumber's number is 555-1234."
        *"Add a note titled 'Vacation Ideas' with the content 'Visit the Grand Canyon'."
        *"Update the note 'Meeting Notes' with 'Discuss budget'."
    *These notes act as the assistant's long-term memory for specific facts you provide. You can view and manage them easily through the Web Interface.

* **Ingest Documents (Files and URLs):**
    * **From URLs:**Ask the assistant to "Save this page for later: [Full URL]" or "Index this article: [Full URL] with title 'My Article Title'". If you don't provide a title, the assistant will try to extract one automatically.
    * **From Files:**You can upload files (like PDFs, text files, etc.) directly through the Web Interface on the "Upload Document" page. The assistant will then process and index these files so you can search their content later.

* **Schedule Follow-ups & Recurring Actions:**
    *If you're discussing something and want the assistant to bring it up again later, you can ask: "Remind me about this tomorrow morning.", "Check back with me on this topic in 3 hours." The assistant will generally send a message back to the chat at the specified time to continue the conversation, even if you've sent other messages in the meantime.
    *Beyond simple follow-ups, the assistant can schedule tasks to happen regularly. For example, you could ask it to "Send a reminder every Sunday evening to take out the bins."

    * **Quick Reminders:**There's now a dedicated reminder feature for simple time-based reminders:
        *"Remind me to call the dentist in 2 hours"
        *"Set a reminder for tomorrow at 3pm to pick up groceries"
        *"Remind me about the meeting at 4:30 PM"

    * **Managing Scheduled Tasks:**You can view and manage all scheduled tasks:
        *"Show me my pending callbacks" - Lists all scheduled tasks and reminders
        *"Cancel the daily weather update" - The assistant will find and cancel matching tasks
        *"Stop all recurring tasks about X" - The assistant will cancel all matching instances
        *For recurring tasks, each future instance is listed separately and can be cancelled individually
    *If a scheduled task fails, you can often retry it manually from the "Tasks" page in the Web Interface.

* **Understand Photos:**
    *Send a photo directly in the chat along with your question (in the same message): "What kind of flower is this?", "Can you describe what's in this picture?" (Support for other file types may be available).

* **Interact with Your Smart Home (Home Assistant):**
    *If your family uses Home Assistant and it's connected to the assistant, you can control devices with your voice:
        *"Turn on the kitchen lights."
        *"Is the garage door closed?"
        *"Set the thermostat to 70 degrees."
        *"What's the temperature in the baby's room?"
    *The assistant now knows your location and can provide context-aware responses:
        *It knows who is home and who is away
        *It can tell you distances to known locations (like work or school)
        *It tracks detailed location information when available

    * *Note:*This depends on how Home Assistant is set up. You'll need to use the names of your lights, switches, sensors, etc., as they are defined in your Home Assistant configuration.

* **Monitor Events and Get Automated Notifications:**
    *The assistant can now watch for specific events and notify you when they happen:
        *"Let me know when Andrew arrives home"
        *"Alert me if the garage door opens after 10pm"
        *"Watch for when the washing machine finishes"
        *"Notify me when any new documents are indexed"
    *You can manage these event listeners:
        *"List all my event listeners"
        *"Disable the garage door alert"
        *"Delete the washing machine listener"
    *Test conditions before creating listeners:
        *"Show me recent events from home assistant"
        *"Test if person.andrew state changes to 'Home' would have triggered in the last day"

## 4. How the Assistant Stays Informed

The assistant learns and gets information from a few places:

* **You Tell It:**When you use commands like "Remember:" or "Add Note:".
* **Connected Calendars:**It automatically checks any shared family calendars that have been linked (like Google Calendar, iCloud Calendar, etc.) for upcoming events.
* **Recent Conversation:**It remembers the last few messages exchanged in your chat to understand the context of your current request.
* **Stored Documents:**It can search and retrieve information from notes you've added, and potentially from emails or files you've uploaded or forwarded (depending on setup).
* **Smart Home Events:**If connected to Home Assistant, the assistant can now:
    *Track who is home and their locations in real-time
    *Monitor device states and sensor readings
    *Watch for specific events you've asked it to track

* **System Events:**The assistant monitors its own operations, including when documents are indexed, tasks complete, or errors occur.

## 5. Automatic Features

* **(Future) Daily Brief:**
    *The plan is to have the assistant automatically send a "Daily Brief" message each morning via Telegram.
    *This brief would likely include a summary of the day's calendar events, reminders (once that feature is added), and perhaps the weather forecast.
    *This feature will use the assistant's ability to run scheduled tasks automatically.

* **Scheduled Reminders:**
    *You can ask the assistant to schedule reminders using its task scheduling feature. For example: "Schedule a task to remind me about 'Take out bins' every Sunday at 7 PM."
    *The assistant will then send you a message in the chat at the scheduled time(s). This uses the same mechanism as the "Schedule Recurring Actions" feature mentioned earlier.

## 6. Using the Web Interface

While most interaction happens via Telegram, the web interface is useful for specific tasks. The interface has been reorganized for better navigation with grouped sections.

* **Accessing it:**`{{ SERVER_URL }}` (This link will be replaced with the actual URL).
* **Navigation:**The web interface is now organized into clear sections:
    * **Information**- View and manage your notes, documents, and conversation history
    * **Operations**- Access background tasks and tool testing
    * **Settings**- Manage API tokens and other configuration
* **What it's for:**
    * **Viewing/Managing Notes:**The Notes page has been enhanced with:
        *A clean, organized list of all your notes
        *Easy editing - click on any note to modify its content
        *Control whether notes are automatically included in conversations
        *Delete notes that are no longer needed
        *Search through notes quickly

    * **Document Management:**The new Documents section provides:
        *A comprehensive list of all indexed documents
        *Document details including type, source, and metadata
        *Direct links to view full document content
        *Search capabilities across all document types

    * **Viewing History:**Look back through past conversations the assistant has had (across different chats, if configured).
    * **Viewing Background Tasks:**See a log of tasks the assistant has performed automatically in the background (like fetching calendar updates or future scheduled actions). You can also manually retry failed tasks from this page.
    * **Searching Documents:**Use the "Vector Search" page to search through all indexed documents (notes, emails, uploaded files, web pages). Results are grouped by document, and you can click to see a "Document Detail View" with complete document content. The detail view displays the full text at the top for easy reading, along with all metadata and search snippets. Even documents that were too large to fully index for search are displayed in their entirety.
    * **Uploading Documents:**Use the "Upload Document" page to add new files (PDFs, text files, etc.) for the assistant to index and learn from.
    * **Managing API Tokens:**If you need programmatic access to the assistant, you can manage your API tokens on the "API Tokens" page under "Settings".
    * **Tool Testing:**A new "Tools" page allows developers to test and debug tool interactions directly from the web interface.

## 7. Tips for Best Results

* **Be Clear:**The more specific your request, the better the assistant can understand and help.
* **Use "Remember" for Facts:**For specific pieces of information you want recalled later (like numbers, addresses, instructions), use the "Remember:" or "Add Note:" command.
* **Managing Notes:**You can now use more natural language to work with notes:
    *"Get the Wi-Fi password note" instead of searching
    *"List all my notes" to see everything at once
    *"Delete the old shopping list" to remove outdated notes

* **Setting Up Event Listeners:**When creating event listeners:
    *Start by exploring what events are available: "Show me recent home assistant events"
    *Test your conditions before creating the listener: "Test if entity_id equals 'person.andrew' would match recent events"
    *Be specific with field names - use the exact names you see in the event data
    *You can filter by event type: "Test if event_type equals 'state_changed' and entity_id equals 'person.andrew'"

* **Reply Directly:**If you're responding to something the assistant just said, use Telegram's "Reply" feature so it knows exactly what message you're referring to. This is especially helpful if the assistant was using a special mode (activated by a slash command), as it helps keep the conversation in that mode.
* **Provide Full URLs:**When asking about web content, always include the full address (e.g., `https://www.example.com/article`).
* **Use Correct Smart Home Names:**For controlling Home Assistant devices, use the exact names configured in your Home Assistant setup (e.g., "Living Room Lamp", "Downstairs Thermostat"). If you're unsure, ask the person who manages your Home Assistant setup.
* **Use Slash Commands:**For specialized tasks like complex web browsing (e.g., `/browse find travel options to Paris for next June`) or in-depth research (e.g., `/research Tell me about the history of Python`), using the appropriate slash command can provide more focused and effective responses.

## 8. Troubleshooting & Help

* **Calendar Modifications:**If you ask to modify or delete an event, the assistant might first ask you to clarify which event using a search ("Find the dentist appointment") and will then ask you to confirm the action via buttons in the chat.
* **Unknown Commands:**If you type a command the assistant doesn't recognize (e.g., `/someunknowncommand`), it will now reply with a "command not recognized" message.
* **Switching Modes or Asking for Confirmation:**To best handle your request, the assistant might sometimes switch to a different specialized mode or ask for your permission to use one (e.g., "Is it okay to use the web browser for this?"). This is normal and helps it use the most appropriate tools.
* **Event Listeners:**If an event listener isn't triggering as expected:
    *Use "Show me recent events from [source]" to see what events are being captured
    *Use the test tool to check if your conditions would match recent events
    *Make sure you're using the exact field names from the event data (use dot notation for nested fields like "new_state.state")

* **Connection Issues:**The assistant now automatically reconnects to Home Assistant and other services if the connection is lost. You may see brief interruptions in event monitoring during reconnection.
* **If it doesn't understand:**Try rephrasing your request. Sometimes slightly different wording makes a big difference.
* **If it makes a mistake or gives wrong information:**You can often correct it by giving it the right information ("Actually, the appointment is at 3 PM") or by updating a relevant note via the Web UI or a command ("Update the note 'Plumber Number' with content '555-9876'").
* **If you need more help:**Contact the family member who set up and manages the assistant for your family. They can help with configuration issues or more complex problems.

We hope you find your family assistant helpful!
