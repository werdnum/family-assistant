# Your Family Assistant - User Guide

Welcome to your personal family assistant! This guide explains how to interact with the assistant and what it can do to help manage your family's information and schedule.

## 1. Introduction: Meet Your Family Assistant

**What is it?** Think of this as a central helper designed to keep track of shared family information, schedules, notes, and even interact with some of your home devices.

**What's the goal?** To simplify managing the details of family life by having one place to ask questions, store information, and get updates.

**How does it work?** You interact with the assistant primarily by chatting with it on Telegram, just like talking to a person. It understands your requests and uses its knowledge and connected services to respond or perform actions.

## 2. Getting Started: How to Talk to the Assistant

*   **Telegram (Primary):**
    *   Find the bot contact in your Telegram app (the person who set it up will tell you its name).
    *   Start a chat and simply send messages with your questions or requests.

*   **Web Interface (Secondary):**
    *   There's also a web page you can use for certain tasks, especially managing notes.
    *   Access it here: `[Link to Web UI]` (Ask the administrator for the correct link).

*   **Email (Future):**
    *   Soon, you might be able to forward emails (like flight confirmations or event invitations) to a special address so the assistant can automatically store the information. Stay tuned!

## 3. What Can the Assistant Do For You? (Core Features)

You can ask the assistant a wide variety of things:

*   **Answer Your Questions:**
    *   **About upcoming events:** "What's happening tomorrow?", "Do we have anything scheduled next Saturday?", "List events for the next 14 days." (The assistant reads from connected family calendars.)
    *   **About things you've told it (Notes):** "What was the Wi-Fi password?", "Remind me about the gift ideas we saved.", "Where did we put the spare keys?" (It uses the notes you've asked it to remember.)
    *   **Add/Modify/Delete Calendar Events:** You can manage your calendar directly:
        *   **Add:** "Add dentist appointment for June 5th at 10 AM." or "Schedule 'Team Lunch' tomorrow from 12 PM to 1 PM."
        *   **Search:** "Are there any events next Tuesday?", "Find the dentist appointment in June." (This helps find events before modifying/deleting).
        *   **Modify:** "Change the 'Team Lunch' to 12:30 PM." (Requires finding the event first. You will be asked to confirm the change.)
        *   **Delete:** "Delete the 'Dentist Appointment' on June 5th." (Requires finding the event first. You will be asked to confirm the deletion.)
    *   **About the current time/date:** "What time is it?", "What day is it today?" (Uses its built-in time service.)
    *   **About web content:** "Can you summarize this article: [Full URL]?", "What's the main point of this webpage: [Full URL]?" (Provide the complete web address starting with `http://` or `https://`. Uses its web fetching service.)
    *   **Search Your Documents:** "Search my notes for 'plumber number'.", "Find emails about the flight booking.", "Look for documents related to 'insurance policy'." (The assistant can search through notes and other documents it has access to. If it finds relevant documents, it will list them.)
    *   **Retrieve Full Documents:** After a search, if the assistant finds a document (e.g., "Document ID 123: Insurance Policy Scan"), you can ask: "Show me the full content of document 123."
    *   **General knowledge & web searches:** "Search the web for reviews of the new park.", "Who won the game last night?", "Find me a recipe for banana bread." (Uses the Brave Search service to find information online.)

*   **Remember Things (Notes):**
    *   Tell it to save information permanently:
        *   "Remember: The plumber's number is 555-1234."
        *   "Add a note titled 'Vacation Ideas' with the content 'Visit the Grand Canyon'."
        *   "Update the note 'Meeting Notes' with 'Discuss budget'."
    *   These notes act as the assistant's long-term memory for specific facts you provide. You can view and manage them easily through the Web Interface.

*   **Schedule Follow-ups:**
    *   If you're discussing something and want the assistant to bring it up again later, you can ask: "Remind me about this tomorrow morning.", "Check back with me on this topic in 3 hours." The assistant will send a message back to the chat at the specified time to continue the conversation.
*   **Schedule Recurring Actions:** Beyond simple follow-ups, the assistant can schedule tasks to happen regularly. For example, you could ask it to "Send a reminder every Sunday evening to take out the bins." (This capability is also used for planned features like the Daily Brief).
*   **Understand Photos:**
    *   Send a photo directly in the chat along with your question (in the same message): "What kind of flower is this?", "Can you describe what's in this picture?"

*   **Interact with Your Smart Home (Home Assistant):**
    *   If your family uses Home Assistant and it's connected to the assistant, you can control devices with your voice:
        *   "Turn on the kitchen lights."
        *   "Is the garage door closed?"
        *   "Set the thermostat to 70 degrees."
        *   "What's the temperature in the baby's room?"
    *   *Note:* This depends on how Home Assistant is set up. You'll need to use the names of your lights, switches, sensors, etc., as they are defined in your Home Assistant configuration.

## 4. How the Assistant Stays Informed

The assistant learns and gets information from a few places:

*   **You Tell It:** When you use commands like "Remember:" or "Add Note:".
*   **Connected Calendars:** It automatically checks any shared family calendars that have been linked (like Google Calendar, iCloud Calendar, etc.) for upcoming events.
*   **Recent Conversation:** It remembers the last few messages exchanged in your chat to understand the context of your current request.
*   **Stored Documents:** It can search and retrieve information from notes you've added, and potentially from emails or files you've uploaded or forwarded (depending on setup).

## 5. Automatic Features

*   **(Future) Daily Brief:**
    *   The plan is to have the assistant automatically send a "Daily Brief" message each morning via Telegram.
    *   This brief would likely include a summary of the day's calendar events, reminders (once that feature is added), and perhaps the weather forecast.
    *   This feature will use the assistant's ability to run scheduled tasks automatically.

*   **Scheduled Reminders:**
    *   You can ask the assistant to schedule reminders using its task scheduling feature. For example: "Schedule a task to remind me about 'Take out bins' every Sunday at 7 PM."
    *   The assistant will then send you a message in the chat at the scheduled time(s). This uses the same mechanism as the "Schedule Recurring Actions" feature mentioned earlier.

## 6. Using the Web Interface

While most interaction happens via Telegram, the web interface is useful for specific tasks.

*   **Accessing it:** `[Link to Web UI]` (Ask the administrator for the correct link)
*   **What it's for:**
    *   **Viewing/Managing Notes:** This is the best place to see a list of all the notes the assistant has saved. You can easily read, edit the content, or delete notes that are no longer needed.
    *   **Viewing History:** Look back through past conversations the assistant has had (across different chats, if configured).
    *   **Viewing Background Tasks:** See a log of tasks the assistant has performed automatically in the background (like fetching calendar updates or future scheduled actions).

## 7. Tips for Best Results

*   **Be Clear:** The more specific your request, the better the assistant can understand and help.
*   **Use "Remember" for Facts:** For specific pieces of information you want recalled later (like numbers, addresses, instructions), use the "Remember:" or "Add Note:" command.
*   **Reply Directly:** If you're responding to something the assistant just said, use Telegram's "Reply" feature so it knows exactly what message you're referring to.
*   **Provide Full URLs:** When asking about web content, always include the full address (e.g., `https://www.example.com/article`).
*   **Use Correct Smart Home Names:** For controlling Home Assistant devices, use the exact names configured in your Home Assistant setup (e.g., "Living Room Lamp", "Downstairs Thermostat"). If you're unsure, ask the person who manages your Home Assistant setup.

## 8. Troubleshooting & Help

*   **Calendar Modifications:** If you ask to modify or delete an event, the assistant might first ask you to clarify which event using a search ("Find the dentist appointment") and will then ask you to confirm the action via buttons in the chat.
*   **If it doesn't understand:** Try rephrasing your request. Sometimes slightly different wording makes a big difference.
*   **If it makes a mistake or gives wrong information:** You can often correct it by giving it the right information ("Actually, the appointment is at 3 PM") or by updating a relevant note via the Web UI or a command ("Update the note 'Plumber Number' with content '555-9876'").
*   **If you need more help:** Contact the family member who set up and manages the assistant for your family. They can help with configuration issues or more complex problems.

We hope you find your family assistant helpful!
