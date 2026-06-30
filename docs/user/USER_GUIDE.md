# Your Family Assistant - User Guide

Welcome to your personal family assistant! This guide explains how to interact with the assistant
and what it can do to help manage your family's information and schedule.

## 1. Introduction: Meet Your Family Assistant

**What is it?** Think of this as a central helper designed to keep track of shared family
information, schedules, notes, and even interact with some of your home devices.

**What's the goal?** To simplify managing the details of family life by having one place to ask
questions, store information, and get updates.

**How does it work?** You interact with the assistant primarily by chatting with it on Telegram,
just like talking to a person. It understands your requests and uses its knowledge and connected
services to respond or perform actions.

## 2. Getting Started: How to Talk to the Assistant

- **Telegram (Primary):** \*Find the bot contact in your Telegram app (the person who set it up will
  tell you its name). \*Start a chat and simply send messages with your questions or requests. \*You
  can also use **slash commands** to activate specialized modes for certain tasks. For example,
  typing `/browse` before your query activates a powerful web browsing mode for complex web tasks or
  research, while `/research` focuses the assistant on in-depth research questions. For
  investigations that need the most thorough, multi-source synthesis (at the cost of longer
  latency), use `/research_max` instead. If you send a follow-up message while the assistant is
  still working through tool calls, it will apply that message to the current response when it can.
  Send `/interrupt` to stop the active Telegram request.

- **Web Interface (Secondary):** \*There's also a web interface for various tasks: \*Chat directly
  with the assistant with real-time streaming responses. \*Managing notes. \*Viewing and filtering
  conversation history across all interfaces (Telegram, Web, Email). \*Managing background tasks.
  \*Uploading documents for the assistant to learn from. \*Managing API tokens for programmatic
  access. \*Searching indexed documents. \*Supports dark mode for comfortable viewing. \*Access it
  here: `{{ SERVER_URL }}` (This link will be replaced with the actual URL). The interface
  automatically opens to the chat page for quick access.

  - **Stop or steer a reply while it's working:** While the assistant is generating a response, the
    chat box doubles as a steering box. Leave it empty and the Send button becomes a **Stop** button
    — click it to halt the current response immediately (it's marked "Stopped", not an error). Or
    type a quick course-correction into the same box (for example, "actually, focus on next week")
    and the button becomes **Steer** — send it and the assistant folds your note into the work it's
    already doing without starting over. Native iOS Chat has the same controls while a reply is
    running. This is the web and iOS equivalent of Telegram's `/interrupt` and mid-response
    follow-up messages.

  ![Landing Page](../../screenshots/desktop/landing-page.png) *The Family Assistant landing page
  provides quick access to all major features*

- **Email:** If configured by the operator, forward order confirmations, tickets, travel bookings,
  school notices, or invitations to your assistant email address. The assistant can summarize the
  email, identify useful actions, and reply by email. Calendar changes, saved notes, reminders, and
  messages to other Family Assistant users require confirmation in a trusted interface such as
  Telegram or the Web UI before they execute. When a forwarded email creates a pending confirmation,
  the assistant sends inline approval buttons to your primary Telegram chat if one is configured.
  Email replies are sent only when your forwarding sender address is mapped to your account by the
  operator.

## 3. What Can the Assistant Do For You? (Core Features)

You can ask the assistant a wide variety of things:

- **Answer Your Questions:**

  - **About upcoming events:** "What's happening tomorrow?", "Do we have anything scheduled next
    Saturday?", "List events for the next 14 days." (The assistant reads from connected family
    calendars.)
  - **About things you've told it (Notes):** "What was the Wi-Fi password?", "Remind me about the
    gift ideas we saved.", "Where did we put the spare keys?" (It uses the notes you've asked it to
    remember.)
  - **Add/Modify/Delete Calendar Events:** You can manage your calendar directly:
    - **Add:** "Add dentist appointment for June 5th at 10 AM." or "Schedule 'Team Lunch' tomorrow
      from 12 PM to 1 PM."
    - **Search:** "Are there any events next Tuesday?", "Find the dentist appointment in June."
      (This helps find events before modifying/deleting).
    - **Modify:** "Change the 'Team Lunch' to 12:30 PM." (Requires finding the event first. You will
      see a confirmation dialog to approve the change.)
    - **Delete:** "Delete the 'Dentist Appointment' on June 5th." (Requires finding the event first.
      You will see a confirmation dialog to approve the deletion.)
  - **About the current time/date:** "What time is it?", "What day is it today?" (Uses its built-in
    time service.)
  - **About the weather:** "What's the weather like today?", "Will it rain tomorrow in London?"
  - **About locations or directions:** "Find coffee shops near me.", "How do I get to the Eiffel
    Tower?" (If enabled, uses Google Maps.)
  - **About web content:** "Can you summarize this article: `[Full URL]`?", "What's the main point
    of this webpage: `[Full URL]`?" (For simple page summaries, the assistant can often fetch the
    content directly. If it has trouble or if the page is very complex (e.g., requires logins or
    interactions), you can try prefacing your request with `/browse`. Always provide the complete
    web address starting with `http://` or `https://`.)
  - **Search Your Documents:** "Search my notes for 'plumber number'.", "Find emails about the
    flight booking.", "Look for PDF documents related to 'insurance policy'." (The assistant can
    search through notes, indexed emails, PDFs, and other documents it has access to. Search results
    will be grouped by document, showing relevant snippets.)
  - **Manage Your Notes:** The notes system has been enhanced with powerful new features:
    - **Get specific notes:** "Get the note titled 'Wi-Fi Password'", "Show me the grocery list
      note"
    - **List all notes:** "List all my notes", "Show me all notes"
    - **Delete notes:** "Delete the note 'Old Shopping List'"
    - **Automatic indexing:** All notes are now automatically indexed for better search capabilities
    - **Smart inclusion:** Notes can be marked to automatically include in conversations when
      relevant
    - **Note visibility:** The assistant shows which notes are excluded from its context - when you
      have notes marked as `include_in_prompt=False`, their titles are listed in the system prompt
      so you know they exist but aren't loaded into every conversation for efficiency
  - **Retrieve Full Documents:** After a search, if the assistant finds a document (e.g., "Document
    ID 123: Insurance Policy Scan"), you can ask: "Show me the full content of document 123." You
    can also click on search results in the Web UI to see a detailed view of the document. The
    document detail view now shows the complete text content at the top of the page, making it easy
    to read the full document without having to reconstruct it from search snippets. Large documents
    (like PDFs or long web pages) are fully accessible even if they were too large to process for
    search indexing.
  - **General knowledge & web searches:** "Search the web for reviews of the new park.", "Who won
    the game last night?", "Find me a recipe for banana bread." (The assistant can search the web
    for information using its default search capabilities. For more complex web research that might
    involve navigating multiple pages or interacting with sites, you can use the `/browse` command
    followed by your research query, e.g.,
    `/browse find recent reviews for the XZ-100 camera and compare its features to the YZ-200`. Uses
    the Brave Search service for some searches.)
  - **Run Automation Scripts:** You can ask the assistant to execute scripts for complex automation:
    \*"Execute a script that finds all TODO notes and creates a summary" \*"Run a script to create
    prep notes for tomorrow's meetings" \*"Write and execute a script that searches for project
    updates and emails me a digest" \*See the [Scripting Guide](scripting.md) for more details on
    what scripts can do.
  - **Script Library:** Save reusable scripts by name and run them later without rewriting: \*"Save
    this script as 'daily-digest'" \*"Run the 'daily-digest' script" \*"List my saved scripts"
    \*Stored scripts can accept parameters and can be referenced by automations, so you can create a
    script once and trigger it on a schedule or in response to events.
  - **Test Scripts Safely Before Real Actions:** You can ask the assistant to test a script against
    realistic tool behavior without actually running state-changing or external-communication tools:
    \*"Test this script with simulated tool outputs before I run it for real" \*"Use the real
    read-only tools, but simulate reminder scheduling and note writes" \*"Check whether this script
    would survive real tool outputs" \*The test harness uses the real tool registry, keeps read-only
    tools real by default, simulates action tools with realistic values, and returns a transcript of
    which calls were real vs simulated.

- **Email the Assistant:** If email intake actions are enabled, you can email the assistant directly
  or forward something — an order confirmation, ticket purchase, school notice — and ask it to do
  something with it (or let it summarise and offer to save useful bits). For example, a soccer
  ticket email can become a calendar event or a note. The assistant replies by email like a normal
  chat. Anything that writes data or sends a message (calendar events, notes, reminders, messages to
  other Family Assistant users, fetching a linked document) waits for you to approve it in Telegram
  or the Web UI before it runs. Approvals stay open for 24 hours, so you don't have to react the
  moment the reply lands. Telegram shows inline approval buttons when it is your configured primary
  communication channel. The assistant ignores instructions embedded inside forwarded content — only
  your direct request controls what it does. If the email came through a recipient-only alias
  without a mapped forwarding sender, confirmations can still be created, but the assistant won't
  email a reply.

- **Forward Email to Index a Linked Document:** If the email points to a document you want to keep
  (a shared PDF, a Drive/Dropbox/iCloud link, a long article), the assistant can propose fetching
  and indexing it. The fetch never runs immediately — it lands as a confirmation in Telegram or the
  Web UI, and only after you approve does the assistant download the document and add it to your
  searchable knowledge base. This keeps untrusted senders from being able to push the assistant at
  arbitrary URLs without your sign-off. Real MIME attachments on the email are indexed
  automatically, so the confirmation flow is mainly for link-style attachments.

- **Remember Things (Notes):** \*Tell it to save information permanently: \*"Remember: The plumber's
  number is 555-1234." \*"Add a note titled 'Vacation Ideas' with the content 'Visit the Grand
  Canyon'." \*"Update the note 'Meeting Notes' with 'Discuss budget'." \*"Append to the note
  'Meeting Notes' with 'Also discuss timeline'." (NEW: You can append content to existing notes
  instead of replacing them!) \*These notes act as the assistant's long-term memory for specific
  facts you provide. You can view and manage them easily through the Web Interface.

- **Skills (Reusable Instructions):** Skills are special notes that give the assistant specialized
  knowledge or step-by-step instructions for particular tasks. Unlike regular notes (which store
  facts), skills contain procedural guidance the assistant follows when a topic comes up.

  - **How skills work:** The assistant sees a catalog of available skills (name + short description)
    in every conversation. When a skill is relevant, it loads the full instructions on demand using
    the `get_note` tool. This keeps conversations lightweight while making specialized knowledge
    available.

  - **Built-in skills:** The assistant ships with several built-in skills including browser
    automation, camera integration, image tools, scheduling guidance, and shopping/UCP guidance.
    These work out of the box.

  - **Creating your own skills (DB-based):** Ask the assistant to create a skill by providing YAML
    frontmatter with `name` and `description` fields at the top of the note content:

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

    Notes with this frontmatter appear in the skill catalog instead of the regular notes section.

  - **File-based skills:** Advanced users can place `.md` files with frontmatter in a configured
    directory. File-based skills are loaded at startup and can be overridden by DB-based skills with
    the same name.

  - **Visibility:** Skills support the same visibility labels as regular notes. Add
    `visibility_labels` in the frontmatter to restrict which profiles can see a skill.

- **Shopping and UCP:** For requests like "find me an X online and send me a checkout link," the
  assistant can load the Shopping skill, use browser/search tools for discovery, build a cart, and
  return the merchant checkout URL for you to complete payment yourself. It works with any merchant
  that supports the Universal Commerce Protocol (UCP) — Shopify stores and other UCP merchants
  alike. The assistant discovers each merchant's commerce endpoint from the merchant's own
  `/.well-known/ucp` profile (following a redirect there to the merchant's own site or a trusted
  commerce-platform host), and while browsing it automatically notices when a site supports UCP
  shopping. This includes Shopify stores on their own custom domain, whose commerce endpoint lives
  on a `*.myshopify.com` host — even when the storefront redirects discovery to that shop host, so
  you can just give the assistant the storefront you browsed. The assistant speaks both UCP
  transports — the MCP JSON-RPC transport that Shopify stores use and the REST transport that
  merchants such as THE ICONIC and Adore Beauty advertise — so a merchant's choice of transport is
  transparent to you. Some merchants are checkout-only (they support checkout but not a cart); for
  these the assistant opens a checkout session directly from the selected items instead of building
  a cart first. The server also publishes its own public UCP platform profile at `/.well-known/ucp`.
  Checkout handoff requires signed UCP requests; configure `UCP_SIGNING_KEY_ID` and either
  `UCP_SIGNING_PRIVATE_KEY` or `UCP_SIGNING_PRIVATE_KEY_PATH` with an EC P-256 or P-384 private key.
  The assistant does not complete checkout or collect payment credentials in chat.

- **Ingest Documents (Files and URLs):**

  - **From URLs:** Ask the assistant to "Save this page for later: [Full URL]" or "Index this
    article: [Full URL] with title 'My Article Title'". If you don't provide a title, the assistant
    will try to extract one automatically.
  - **From Files:** You can upload files (like PDFs, text files, etc.) directly through the Web
    Interface on the "Upload Document" page. The assistant will then process and index these files
    so you can search their content later.

- **Schedule Follow-ups & Recurring Actions:** \*If you're discussing something and want the
  assistant to bring it up again later, you can ask: "Remind me about this tomorrow morning.",
  "Check back with me on this topic in 3 hours." The assistant will generally send a message back to
  the chat at the specified time to continue the conversation, even if you've sent other messages in
  the meantime. \*Beyond simple follow-ups, the assistant can schedule tasks to happen regularly.
  For example, you could ask it to "Send a reminder every Sunday evening to take out the bins."

  - **Quick Reminders:** There's now a dedicated reminder feature for simple time-based reminders:
    \*"Remind me to call the dentist in 2 hours" \*"Set a reminder for tomorrow at 3pm to pick up
    groceries" \*"Remind me about the meeting at 4:30 PM" \*"Don't let me forget to submit the
    report" - Creates a reminder with automatic follow-ups if you don't respond

  - **Schedule Script Execution:** You can now schedule scripts to run at specific times or on
    recurring schedules: \*"Schedule a script to clean up old notes every Sunday at midnight" \*"Run
    a script tomorrow at 9am that summarizes all my TODO notes" \*"Execute a script every hour to
    check if any tasks are overdue" \*Scripts run automatically without waking the assistant, making
    them perfect for automated maintenance tasks

  - **Managing Scheduled Tasks:** You can view and manage all scheduled tasks: \*"Show me my pending
    callbacks" - Lists all scheduled tasks, reminders, and script executions \*"Cancel the daily
    weather update" - The assistant will find and cancel matching tasks \*"Stop all recurring tasks
    about X" - The assistant will cancel all matching instances \*"Modify the reminder about the
    dentist to 3pm instead" - Changes the time of a scheduled task \*For recurring tasks, each
    future instance is listed separately and can be cancelled individually \*If a scheduled task
    fails, you can often retry it manually from the "Tasks" page in the Web Interface.

- **Understand Photos:** \*Send a photo directly in the chat along with your question (in the same
  message): "What kind of flower is this?", "Can you describe what's in this picture?" (Support for
  other file types may be available).

- **Create Data Visualizations:** \*The assistant can create charts and graphs from your data using
  the specialized `/visualize` or `/chart` commands: \*"/visualize Create a bar chart showing sales
  by month from this CSV file" (attach a CSV file) \*"/chart Generate a line graph of temperature
  trends" (provide JSON data or attach a data file) \*"/visualize the distribution of categories in
  this dataset" \*These commands activate a specialized data visualization profile that is optimized
  for creating professional charts using Vega/Vega-Lite. The assistant will produce high-quality PNG
  images that you can view, download, or share. You can provide data inline in your message or
  attach CSV/JSON files. The system supports a wide variety of chart types including bar charts,
  line graphs, scatter plots, pie charts, area charts, and more complex visualizations.

- **Generate Videos:** *Create short videos from text descriptions using the latest AI models:*

  - "Generate a video of a futuristic city with flying cars."
  - "Create a video of a puppy playing in the grass, 16:9 aspect ratio."
  - "Make a 4-second video of ocean waves crashing on rocks." *The assistant will generate the video
    and provide a link to view it.*

- **Interact with Your Smart Home (Home Assistant):** \*If your family uses Home Assistant and it's
  connected to the assistant, you can control devices with your voice: \*"Turn on the kitchen
  lights." \*"Is the garage door closed?" \*"Set the thermostat to 70 degrees." \*"What's the
  temperature in the baby's room?" \*The assistant now knows your location and can provide
  context-aware responses: \*It knows who is home and who is away \*It can tell you distances to
  known locations (like work or school) \*It tracks detailed location information when available
  \*Under the hood the assistant can run any Home Assistant action (formerly called a "service
  call"), so it can do things like activating scenes ("activate movie night"), running scripts ("run
  my bedtime script"), playing media, or sending HA notifications — anything Home Assistant itself
  can do.

  - \*Note:\*This depends on how Home Assistant is set up. You'll need to use the names of your
    lights, switches, sensors, etc., as they are defined in your Home Assistant configuration.

- **Monitor Events and Get Automated Notifications:** \*The assistant can now watch for specific
  events and notify you when they happen: \*"Let me know when Alex arrives home" \*"Alert me if the
  garage door opens after 10pm" \*"Watch for when the washing machine finishes" \*"Notify me when
  any new documents are indexed" \*You can manage these automations: \*"List all my automations" or
  "List all my event automations" \*"Disable the garage door alert" \*"Delete the washing machine
  automation" \*Test conditions before creating automations: \*"Show me recent events from home
  assistant" \*"Test if person.alex state changes to 'Home' would have triggered in the last day"
  \*You can also manage automations through the Web UI: navigate to the Automations section to view
  all automations, see their execution history, and modify their conditions or scripts

- **Automated Script Actions for Events:** \*For simple, deterministic actions, you can now create
  script-based event automations that run instantly without waking the assistant: \*"Run a script to
  log all motion events to a note when motion is detected" \*"Create a script that sends me a
  Telegram message when the temperature goes above 25°C during business hours" \*"Set up a script to
  track daily energy usage in a note whenever the meter reading changes" \*Benefits of script-based
  automations: \*Much faster execution (no LLM processing delay) \*No API costs \*Predictable,
  repeatable behavior \*Perfect for logging, simple notifications, and data collection \*Managing
  script automations: \*"Show me the script for my temperature alert" \*"Test this script with a
  sample temperature event: [provide script code]" \*"Convert my garage door automation to use a
  script instead" \*If you want to validate a script before it touches live state, ask the assistant
  to test it with simulated tools first. \*Automation scripts run with the same tools the assistant
  had when it created them, and that tool set is checked when the automation is saved — so a script
  that passes validation will have the tools it needs when it later runs. If a script calls an
  action that normally needs your approval (like deleting a calendar event), it won't run silently
  in the background: a confirmation lands in Telegram or the Web UI, and the action only happens
  once you approve it.

## 4. Working with Attachments

The assistant can work with various types of attachments (images, documents, files) that you send or
that are generated by tools. Here's everything you need to know about attachment workflows:

### Sending Attachments to the Assistant

- **Images and Photos:** Send images directly in your message to ask questions about them:

  - "What kind of flower is this?" (attach a photo)
  - "Can you describe what's in this picture?"
  - "Read the text from this document image"
  - **Albums:** You can also attach a Telegram album (multiple photos in one message). The assistant
    treats the whole album as a single message and answers your caption once with all the photos in
    mind, rather than replying separately to each photo.

- **Documents:** Upload PDF files, text files, and other documents:

  - Via Telegram: Send the file directly in your chat message
  - Via Web Interface: Use the "Upload Document" page for batch uploads

- **File Types Supported:** The assistant can handle various file types including:

  - Images: JPEG, PNG, GIF, WebP, BMP, TIFF
  - Documents: PDF, plain text, Markdown, JSON, CSV
  - All attachments have size limits (typically 20MB for images, 100MB for other files)

### How Attachments Work

**Unique IDs:** Every attachment gets a unique identifier (UUID) that the assistant can reference
throughout your conversation. This means:

- You can refer back to attachments you sent earlier
- The assistant can pass attachments between different tools
- Attachments remain available for the duration of your conversation

**Conversation Scoping:** Attachments are private to each conversation:

- Only you can access attachments you upload in your conversation
- The assistant cannot share your attachments with other users without explicit action
- Each conversation maintains its own attachment collection

### Attachment Workflows

**Image Analysis:** Send an image and ask the assistant to:

- Describe what it sees
- Extract text (OCR)
- Identify objects, people, or scenes
- Answer specific questions about the image content

**Image Generation and Editing:** Ask the assistant to create or transform images:

- Generate a new image from a text description
- Edit one attached image, such as removing an object or changing the style
- Combine multiple attached images, such as placing a subject from one image into another scene
- Use one image as the primary image and another as a visual style or reference image

**Document Processing:** Upload documents to have the assistant:

- Summarize the content
- Extract specific information
- Search for particular topics
- Index the content for future searches

**Forwarding Attachments:** The assistant can send attachments to other family members:

- "Send this image to John" (using the `send_message_to_user` tool)
- Attachments from your conversation can be shared with other known users
- The assistant will always confirm before sending attachments to others

**Cross-Tool Workflows:** Attachments can be passed between different tools:

- Take a camera snapshot, then analyze it with vision tools
- Process an uploaded document, then save extracted information as notes
- Edit an image, then send the result to another user

### Getting Attachment Information

You can ask the assistant about any attachment:

- "Tell me about this attachment" (reference a specific image/file)
- "What files have I uploaded recently?"
- The assistant can provide metadata like file size, type, upload time, and description

### Security and Privacy

**Access Control:**

- Your attachments are only accessible within your conversation
- The assistant uses secure, non-guessable identifiers for all attachments
- No other users can access your attachments unless explicitly shared

**Data Handling:**

- Attachments are stored securely with proper authentication
- File types and sizes are validated before processing
- All attachment operations are logged for troubleshooting

### Tips for Working with Attachments

- **Be specific:** When asking about images, be clear about what you want to know
- **Use context:** Reference previous attachments in your conversation when relevant
- **File organization:** Consider using descriptive messages when uploading multiple files
- **Size limits:** Very large files may take longer to process or may be rejected

## 5. How the Assistant Stays Informed

The assistant learns and gets information from a few places:

- **You Tell It:** When you use commands like "Remember:" or "Add Note:".

- **Connected Calendars:** It automatically checks any shared family calendars that have been linked
  (like Google Calendar, iCloud Calendar, etc.) for upcoming events.

- **Recent Conversation:** It remembers the last few messages exchanged in your chat to understand
  the context of your current request.

- **Stored Documents:** It can search and retrieve information from notes you've added, and
  potentially from emails or files you've uploaded or forwarded (depending on setup).

- **Smart Home Events:** If connected to Home Assistant, the assistant can now: \*Track who is home
  and their locations in real-time \*Monitor device states and sensor readings \*Watch for specific
  events you've asked it to track

- **System Events:** The assistant monitors its own operations, including when documents are
  indexed, tasks complete, or errors occur.

## 5. Automatic Features

- **(Future) Daily Brief:** \*The plan is to have the assistant automatically send a "Daily Brief"
  message each morning via Telegram. \*This brief would likely include a summary of the day's
  calendar events, reminders (once that feature is added), and perhaps the weather forecast. \*This
  feature will use the assistant's ability to run scheduled tasks automatically.

- **Scheduled Reminders:** \*You can ask the assistant to schedule reminders using its task
  scheduling feature. For example: "Schedule a task to remind me about 'Take out bins' every Sunday
  at 7 PM." \*The assistant will then send you a message in the chat at the scheduled time(s). This
  uses the same mechanism as the "Schedule Recurring Actions" feature mentioned earlier.

## 6. Tool Confirmations

When the assistant needs to perform important actions, you'll be asked to confirm them first:

- **What requires confirmation:** Calendar modifications, file operations, sending messages, and
  other actions that could have significant effects
- **How it works:**
  - In Telegram: You'll see inline buttons to "Approve" or "Deny" the action
  - In the Web Interface: A dialog box will appear with details about the action and options to
    approve or deny
  - On the iOS app: confirmation push notifications are actionable. Long-press (or pull down) the
    notification to "Approve" or "Reject" it directly from the lock screen, or **tap the
    notification** to open a confirmation dialog inside the app that shows the full request and lets
    you approve or reject it there.
  - Pending web approvals are stored durably. If you reload the page or the assistant process
    restarts, the web interface can show the pending approval again and your approval still uses the
    background task queue for execution.
  - If the operator has linked your Telegram and web login to the same account, an action requested
    from Telegram can also appear in the web interface for approval.
  - The assistant will wait for your response before proceeding
- **Background turns can ask too:** When the assistant acts without you in the chat — a scheduled
  reminder or callback firing, or a delegated task reporting back its result — and it needs to do
  something that requires approval, it no longer just gives up. It records the request as a pending
  confirmation addressed to you, sends it to your primary channel, and finishes its turn; the action
  runs later once you approve, just like the email and automation-script flows above. (For older
  reminders scheduled before this was added, the assistant has no record of who to ask, so a
  confirm-gated action is reported as "not run" instead.)
- **Why this matters:** This gives you full control over what the assistant does and prevents
  unintended actions

## 7. Using the Web Interface

While most interaction happens via Telegram, the web interface provides a responsive experience for
various tasks with dark mode support and mobile optimization.

- **Accessing it:**`{{ SERVER_URL }}` (This link will be replaced with the actual URL). The
  interface automatically opens to the chat page for immediate access.

- **iOS App:** If the native iOS app is installed, it opens Family Assistant after secure sign-in.
  The app is organized into four tabs along the bottom: **Chat**, **Notes**, **Documents**, and
  **More**. Chat and Notes are native screens; Documents and the items under More open the
  corresponding pages inside the app. Native Chat shares the same conversation history as the
  browser, streams replies as they are generated, supports stopping or steering a running reply,
  supports profile switching (picking a profile starts a fresh conversation in it, and reopening an
  earlier conversation resumes the profile it was started in so its prior messages stay in the
  assistant's context), shows Markdown and tool calls, handles approve/reject confirmations, and can
  upload images, PDFs, plain text, and Markdown files up to 100 MB. From the iOS share sheet or
  Files "Open In" flow, sharing a supported file to Family Assistant opens a new Chat draft and
  attaches the file so you can add a message before sending. A very long message (or a large tool
  result) renders a section at a time with a **Show more** control, so the thread stays responsive
  no matter how big the content is. The Chat tab opens directly to a fresh composer when there is no
  recent conversation to restore, and long-pressing the app icon shows a **New Chat** quick action
  that jumps straight to a blank composer. You can search, read, create, edit, and delete notes on
  iOS, including changing whether a note is included in the assistant's system prompt. The **More**
  tab gathers the long-tail destinations — Voice, Events, History, Automations, Tools, and more —
  and a single **Settings** screen with notification controls and sign-out. On iOS, Voice opens as a
  native screen: it asks for microphone permission, shows a microphone level meter while audio is
  being captured, and reports an error instead of connecting silently if the microphone pipeline
  does not start. Use the tab bar to move between sections; each tab remembers where you were. When
  enabled, iOS notifications can open the relevant tab or page, and confirmation notifications are
  actionable: they include approve/reject actions, and tapping the notification opens an in-app
  confirmation dialog showing the full request.

- **Siri & Shortcuts (iOS):** The iOS app adds actions you can run with Siri or the **Shortcuts**
  app — no setup required. Just say or run:

  - **"Ask Family Assistant…"** — ask a question and hear/read the reply inline. A **Continue in
    app** button opens the full conversation (useful if the assistant needs you to approve an
    action).
  - **"Capture this in Family Assistant"** — send a piece of text or a link and the assistant saves
    and files it for you. Add this to a Shortcut with *Show in Share Sheet* turned on to send things
    to the assistant from other apps (Safari, Notes, etc.). Because captured web pages and emails
    are outside content, the assistant handles them in a restricted, safe mode: it can read your
    information to file things sensibly but will ask you to approve any changes it wants to make
    (you'll get a confirmation you can approve from the app).
  - **"Add a note to Family Assistant"** — quickly create a note by title and content.
  - **"Open Family Assistant chat"** — open the app on the Chat tab, optionally starting a new
    conversation with a message you provide.

  These actions also appear as building blocks in the Shortcuts app, so you can combine them with
  other automations (for example, capture the current Safari page on a tap). You must be signed in
  to the app first; if you are signed out, the shortcut will ask you to open the app and sign in.

- **Chat Features:**

  - Real-time streaming responses - see the assistant's replies as they're being generated
  - Stop or steer a running reply from the web chat or native iOS Chat
  - Compact tool usage display - completed tool calls are summarized in collapsible groups
  - Easy conversation management and switching - reopening an existing conversation automatically
    resumes it under the profile it was started with, so a follow-up message keeps the earlier
    context. Picking a profile or starting a new chat uses your preferred profile (the last one you
    selected in the picker).
  - Native iOS conversation list, profile picker, attachment previews, file downloads, and pending
    approval banners
  - Clear message formatting and display

  ![Chat Interface](../../screenshots/desktop/chat-empty.png) *The web chat interface provides
  real-time streaming responses and conversation management*

  ![Collapsed Tool Calls](../../screenshots/desktop/chat-tool-calls-collapsed.png) *Completed tool
  calls stay collapsed by default while preserving access to the details*

- **Navigation:** The web interface features a dropdown menu organized into clear sections:

  - **Information** - View and manage your notes, documents, and conversation history with enhanced
    filtering
  - **Operations** - Access background tasks and tool testing
  - **Settings** - Manage API tokens and other configuration

- **What it's for:**

  - **Viewing/Managing Notes:** The Notes page has been enhanced with: \*A clean, organized list of
    all your notes \*Easy editing - click on any note to modify its content \*Control whether notes
    are automatically included in conversations \*Delete notes that are no longer needed \*Search
    through notes quickly

    ![Notes List](../../screenshots/desktop/notes-list.png) *The Notes page shows all your saved
    notes with editing and management options*

    ![Add Note Form](../../screenshots/desktop/notes-add-form.png) *Creating a new note with title,
    content, and visibility options*

  - **Document Management:** The Documents section provides: \*A comprehensive list of all indexed
    documents \*Document details including type, source, and metadata \*Direct links to view full
    document content \*Search capabilities across all document types

    ![Documents Page](../../screenshots/desktop/documents.png) *The Documents page displays all
    indexed documents with search and filtering*

  - **Viewing History:** Look back through past conversations the assistant has had across all
    interfaces (Telegram, Web, Email). Use enhanced filtering options to find specific conversations
    or messages.

    ![History Page](../../screenshots/desktop/history.png) *The History page shows past
    conversations with filtering and search options*

  - **Asking About Past Conversations:** You can ask the assistant to search older conversation
    history directly, such as "what did I say about passports last month?" or "when did you last add
    a calendar event for me?" The assistant can search exact fields like dates, roles, tools,
    attachments, and errors, and can also use semantic search for fuzzy references. Results are
    scoped conservatively to your own history and the current processing context.

  - **Viewing Background Tasks:** See a log of tasks the assistant has performed automatically in
    the background (like fetching calendar updates or future scheduled actions). You can also
    manually retry failed tasks from this page.

    ![Tasks Page](../../screenshots/desktop/tasks.png) *The Tasks page displays background
    operations and scheduled actions*

  - **Searching Documents:** Use the "Vector Search" page to search through all indexed documents
    (notes, emails, uploaded files, web pages). Results are grouped by document, and you can click
    to see a "Document Detail View" with complete document content. The detail view displays the
    full text at the top for easy reading, along with all metadata and search snippets. Even
    documents that were too large to fully index for search are displayed in their entirety.

    ![Vector Search](../../screenshots/desktop/vector-search.png) *The Vector Search page allows
    semantic search across all indexed documents*

  - **Uploading Documents:** Use the "Upload Document" page to add new files (PDFs, text files,
    etc.) for the assistant to index and learn from.

  - **Managing API Tokens:** If you need programmatic access to the assistant, you can manage your
    API tokens on the "API Tokens" page under "Settings".

    ![Settings Page](../../screenshots/desktop/settings.png) *The Settings page for managing API
    tokens and configuration*

  - **Tool Testing:** A "Tools" page allows developers to test and debug tool interactions directly
    from the web interface.

    ![Tools Page](../../screenshots/desktop/tools.png) *The Tools Explorer shows all available tools
    with their descriptions and parameters*

  - **Automations Management:** The Automations page provides: \*A comprehensive list of all
    automations (both event-based and schedule-based) \*Create new automations directly from the UI
    \*Detailed view of each automation's configuration and execution history \*Edit functionality to
    modify automation conditions, scripts, and settings (including changing between LLM callback and
    script action types) \*Live script validation for script-based automations \*Enable/disable and
    delete controls for managing automations \*Filter by automation type (event or schedule) and
    enabled status

    ![Automations Page](../../screenshots/desktop/automations.png) *The Automations page for
    managing event and schedule-based automations*

- **Long-Running Profile Delegation:** For tasks better handled by a specialized assistant profile
  (for example browsing, research, visualization, or complex planning), the assistant may delegate
  the work. Fast delegated work is returned inline. If it takes longer, the assistant gives you a
  `delegation_...` reference and keeps the main conversation available. When the delegated profile
  finishes, it wakes the original assistant profile, which reviews the result and posts the
  follow-up in the same conversation. You can ask for the status of that delegation reference later.
  This works the same way for specialized profiles that run on a separate (remote) agent — they may
  take a while, but the result is still delivered back to this conversation automatically when
  ready. This is separate from spawned worker tasks, which are isolated coding or computing jobs and
  use worker task IDs instead.

- **Push Notifications:** The assistant can notify you even when the app isn't open.

  - **Browser (PWA):** Install the web app to your device and allow notifications to receive browser
    push notifications.
  - **iOS app:** The native iOS app registers for Apple push notifications, so you get alerts
    delivered through the system.
  - **When you're notified:** a new assistant reply arrives in a web conversation, a confirmation is
    waiting for your approval, a background task fails after retrying, or a spawned worker task
    finishes. Long-running delegated profile tasks also notify the conversation when they complete.
    Notifications are sent to every device you've registered.
  - **Closing the app mid-reply:** If you send a message and then close or background the app (web
    or iOS) before the assistant finishes, processing keeps running on the server. When it
    completes, the reply is delivered as a push notification — tap it to jump back to the
    conversation. (You'll only get this push if the connection actually dropped; while you're
    watching the response stream live, no extra notification is sent.)
  - **Reopening after a dropped connection:** If the connection drops while a reply is streaming (a
    flaky network, switching apps, or backgrounding), the app reconnects and continues the same
    reply where it can, or quietly reloads the finished reply from history when it returns — you
    won't be left with a stuck "thinking" bubble or a spurious error for a reply that actually
    succeeded. This self-heals on its own: even on a network that is hostile to live streaming, the
    iOS app keeps catching up the conversation in the background and refreshes again when you bring
    it to the foreground, so you no longer have to pull-to-refresh the conversation list to see a
    reply that finished while you were away.
  - **Opening a conversation while a reply is in progress:** If a turn is already running when you
    open a conversation on iOS — for example one you started on another device — the reply now
    streams in live as it's generated, instead of only appearing once it finishes.

## 8. Tips for Best Results

- **Be Clear:** The more specific your request, the better the assistant can understand and help.

- **Use "Remember" for Facts:** For specific pieces of information you want recalled later (like
  numbers, addresses, instructions), use the "Remember:" or "Add Note:" command.

- **Managing Notes:** You can now use more natural language to work with notes: \*"Get the Wi-Fi
  password note" instead of searching \*"List all my notes" to see everything at once \*"Delete the
  old shopping list" to remove outdated notes

- **Setting Up Event Automations:** You can create event automations either by asking the assistant
  or through the Web UI. When creating event automations: \*Start by exploring what events are
  available: "Show me recent home assistant events" \*Test your conditions before creating the
  automation: "Test if entity_id equals 'person.alex' would match recent events" \*Be specific with
  field names - use the exact names you see in the event data \*You can filter by event type: "Test
  if event_type equals 'state_changed' and entity_id equals 'person.alex'" \*For complex conditions
  (like detecting zone entry/exit or temperature thresholds), use condition scripts: \*"Create an
  automation that detects when I arrive home" (state changes from not 'home' to 'home') \*"Alert me
  when temperature rises above 25°C" (numeric threshold checking) \*"Watch for any motion sensor
  that turns on" (pattern matching with entity_id) \*Choose between two action types:

  - **wake_llm**: Wakes the assistant to handle complex situations requiring reasoning - **script**:
    Runs automated Python scripts for simple, deterministic tasks \*Scripts can also use wake_llm()
    to conditionally wake the assistant with specific context \*Via the Web UI: Navigate to
    Automations and click "Create New Automation" to use the visual form with live script validation

- **Reply Directly:** If you're responding to something the assistant just said, use Telegram's
  "Reply" feature so it knows exactly what message you're referring to. This is especially helpful
  if the assistant was using a special mode (activated by a slash command), as it helps keep the
  conversation in that mode.

- **Provide Full URLs:** When asking about web content, always include the full address (e.g.,
  `https://www.example.com/article`).

- **Use Correct Smart Home Names:** For controlling Home Assistant devices, use the exact names
  configured in your Home Assistant setup (e.g., "Living Room Lamp", "Downstairs Thermostat"). If
  you're unsure, ask the person who manages your Home Assistant setup.

- **Use Slash Commands:** For specialized tasks like complex web browsing (e.g.,
  `/browse find travel options to Paris for next June`), in-depth research (e.g.,
  `/research Tell me about the history of Python`), the most comprehensive deep research (e.g.,
  `/research_max Compare the regulatory landscape for autonomous vehicles across the EU, US, and Japan`),
  complex multi-step reasoning or planning (e.g.,
  `/complex Plan a detailed family vacation considering everyone's schedules and preferences`), or
  engineering diagnostics (e.g., `/engineer Why isn't my daily brief automation triggering?`), using
  the appropriate slash command can provide more focused and effective responses. Use `/research`
  for most questions and reach for `/research_max` when thoroughness matters more than turnaround.

- **Mobile Experience:** The web interface is fully optimized for mobile devices. All features are
  accessible on phones and tablets with responsive design that adapts to your screen size.

## 9. Troubleshooting & Help

- **Calendar Modifications:** If you ask to modify or delete an event, the assistant might first ask
  you to clarify which event using a search ("Find the dentist appointment") and will then show you
  a confirmation dialog (buttons in Telegram, dialog box in Web UI) to approve the action.

- **Unknown Commands:** If you type a command the assistant doesn't recognize (e.g.,
  `/someunknowncommand`), it will now reply with a "command not recognized" message.

- **Interrupting Telegram:** Use `/interrupt` in Telegram to stop the request currently being
  processed in that chat. If there is no active request, the assistant will tell you nothing is
  running.

- **Switching Modes or Asking for Confirmation:** To best handle your request, the assistant might
  sometimes switch to a different specialized mode or ask for your permission to use one (e.g., "Is
  it okay to use the web browser for this?"). This is normal and helps it use the most appropriate
  tools.

- **Event Automations:** If an event automation isn't triggering as expected: \*Use "Show me recent
  events from [source]" to see what events are being captured \*Use the test tool to check if your
  conditions would match recent events \*Make sure you're using the exact field names from the event
  data (use dot notation for nested fields like "new_state.state") \*For condition scripts, test
  them first: "Test this condition script with a sample event: [your script]" \*Remember that
  condition scripts must return a boolean value \*Common issue: Home Assistant sends state_changed
  events even when only attributes change - use condition scripts to detect actual state transitions

- **Connection Issues:** The assistant now automatically reconnects to Home Assistant and other
  services if the connection is lost. You may see brief interruptions in event monitoring during
  reconnection.

- **If it doesn't understand:** Try rephrasing your request. Sometimes slightly different wording
  makes a big difference.

- **If it makes a mistake or gives wrong information:** You can often correct it by giving it the
  right information ("Actually, the appointment is at 3 PM") or by updating a relevant note via the
  Web UI or a command ("Update the note 'Plumber Number' with content '555-9876'").

- **iOS App Errors (TestFlight & beyond):** When the native iOS app shows an error message, it now
  also reports that error to the server automatically, so the family member who manages the
  assistant can see what went wrong without you having to describe it. Uncaught app errors are
  captured the same way (and outright crashes are reported through Apple/TestFlight). Administrators
  can review these reports in the web **Error Logs** page (`/errors`); native iOS reports are tagged
  with a `component_name` and an `is_testflight` flag.

- **Reporting bugs to the developers:** If something seems broken — a tool errors out, data looks
  wrong, or the assistant can't do something it should — you can just say "report this as a bug" (or
  the assistant may do it on its own when it notices a problem). It records the issue in the
  application's error log, where the family member who manages the assistant can review it on the
  web **Error Logs** page (`/errors`) or in the diagnostics export. This only files a report; it
  doesn't fix the problem on its own.

- **If you need more help:** Contact the family member who set up and manages the assistant for your
  family. They can help with configuration issues or more complex problems.

## Additional Resources

- [Scheduling and Task Management Guide](scheduling.md) - Detailed guide on using reminders,
  callbacks, and automated scripts
- [Scripting Guide](scripting.md) - Learn how to write automation scripts for the assistant
- [Browser Automation Guide](browser_automation.md) - Guide to using `/browse` for complex web
  interactions

We hope you find your family assistant helpful!
