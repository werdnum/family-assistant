# One thread, with threads where they earn it

## Status

Approved. Milestones 1 (threads exist) and 2 (Home on web, including the right-hand panel),
milestone 3 (state and echo) on the backend, the web and iOS, milestone 4 (Home on iOS), and
milestone 5 (user overrides, and automations' "new conversation per run" as a thread) on the
backend, the web and iOS implemented. Mockups:
[one-thread-ux/mockups.html](one-thread-ux/mockups.html) (rendered: [iOS](one-thread-ux/ios.png),
[web](one-thread-ux/web.png), [notes](one-thread-ux/notes.png)).

## Problem

The web and iOS clients are organised as a list of separate conversations. That suits a chatbot. It
does not suit a household assistant, which is mostly one ongoing relationship with occasional bigger
jobs:

- A quick question costs a choice: find the right conversation, or start a new one that stays in the
  list forever.
- Long-running work (delegations, browser runs, research) reports back into whichever conversation
  started it. Progress, questions and results from several jobs end up interleaved with unrelated
  chat.
- Switching profile silently starts a new conversation, because history is partitioned by profile.
- Delegated work is only visible as a transient chip (`PendingDelegationsChip`), and once it
  delivers there is nothing left to open.

Telegram already works the other way: one chat, with reply chains as its only structure. People use
it happily for quick questions, but it breaks down when several jobs run in parallel.

## Precedents

- **Meta Muse:** the product is one persistent one-to-one conversation. "Side chats" branch off for
  a single project, and approval cards for consequential actions sit outside the normal chat flow.
- **Grok Bot:** a single "chief of staff" chat delegates work behind the scenes. Threads hang off
  messages behind a chip. The loudest user complaint is that threads are hard to follow from the
  main chat, and users ask for a Slack-style side panel.
- **Claude Code projects:** a parent session spawns child sessions, each shown inline as a card with
  a status dot and an artifact chip. Tapping a card opens the child.
- **Slack:** "Also send to channel" posts a thread's reply into the main stream. The thread keeps
  the detail and the channel keeps the headline.

## The experience

**Home is a single stream and the landing screen.** Quick questions, small edits, reminders,
proactive messages and the morning brief all live there, inline, like a Telegram DM.

**Proactive messages land in Home.** A reminder, callback or automation created on web or iOS
delivers to its owner's Home, whichever conversation created it, except one created in a thread,
which belongs to that thread's work. The destination is written down when it is created and resolved
to a conversation when it fires, so it reaches the Home the owner has then. "Here" remains an
explicit choice. Telegram is unchanged, because a Telegram chat is already its own Home. The "new
conversation per run" option starts a thread off Home at each firing, through the same creation path
as every other thread, and the run's turn happens there; with threads off it is still a separate
conversation. Results of work started in a conversation, such as worker completions and delegation
results, return to that conversation; only proactive messages default to Home.

**A thread is for work that outlives the turn.** That means work that runs in the background, waits
on someone, takes several steps the user will want to follow, or is a project the user will come
back to. The assistant starts a thread and says so in its reply. The user can start one too:
"Continue in a thread" on any message, "Move to thread…" for an existing thread, send-as-thread from
the composer, or "New thread" from the switcher.

**In Home, a thread is one live card.** It shows the title, a state (working, waiting on you, done,
failed), the latest step or question, and an unread count. The card updates in place and never turns
into a stream of progress messages.

**Answering a thread always reaches the thread.** A simple question the thread is waiting on can be
answered from its card, and the answer goes into the thread, where the thread's profile, tools and
browser tab are. Anything richer opens the thread.

**When a thread finishes, its outcome is echoed into Home.** The thread's own final message appears
in Home, attributed to the thread, so Home reads as the whole story at headline level. A thread that
ends on a question has not finished: it is waiting on you, and its card carries the question.

**Navigation follows the platform:**

- **iOS:** Home replaces the conversation list as the landing screen. The list becomes the thread
  switcher, grouped as Waiting on you / Working / Done today / Earlier. Cards and switcher rows push
  into the thread, which is today's chat view.
- **Web:** three panes. The sidebar holds Home and threads grouped by state. Home stays in the
  centre, and an open thread sits in a right-hand panel that can expand to the centre. Narrow
  windows behave like iOS.
- **Telegram:** the private chat is Home. A thread card is a message edited in place, and replying
  to a card routes into that thread.
- **Voice:** a voice session started from the app belongs to that app's Home. A job that becomes a
  thread is announced aloud, and its card appears in Home.

## Model: a thread is a conversation with a parent

The alternative was to surface the existing subconversations, which `delegate_to_service` already
creates and which `message_history` already partitions on. It was rejected for two reasons.

**Subconversations mean a delegate's private working history.** Their request text is written by a
model, they allow one active run each, and they are deliberately excluded from `/messages`,
summaries and the conversation list. Making some of them visible would put an exception into each of
those filters, and every reader would have to tell the two kinds apart.

**Almost everything a thread needs is already keyed per conversation.** That covers:

- stream hub buffers and resume, active turns, cancel and steer, and activity pings
- the ownership check and share links
- the sticky model tier and profile adoption
- summaries and memory review

Both clients already render a conversation, too. Under the subconversation model each of these would
need a new dimension. Under this model they work unchanged.

### The three kinds of conversation

- **Home** is one designated conversation per user per surface, keyed on the user's canonical
  identity so every way one person signs in reaches the same Home. Web and iOS already share an
  interface type and a conversation namespace, so they share one Home. A user's Telegram private
  chat is their Telegram Home. Unifying the two is out of scope (see Deliberate simplifications).
- **A thread** is an ordinary conversation plus a link record. Every thread has one, whether or not
  it grew from an anchor, and the record is what tells a thread from a legacy conversation. It holds
  the parent Home, the anchor turn (if any) and the thread's state.
- **Legacy conversations** have neither a Home role nor a parent. They appear under "Earlier" in the
  switcher, open and continue as they do today, and are never migrated.

**Subconversations are unchanged.** A thread that delegates gets hidden subconversations inside the
thread's conversation, exactly as today. The browser scope is keyed on conversation and
subconversation, so separate threads get separate tabs by construction.

**Each thread keeps its own profile and tier.** Choosing another profile from Home starts a thread
on that profile rather than an unrelated conversation. Home stays on the default profile. In Home, a
tier choice applies only to that turn and does not stick. A request that deserves a stronger model
for the long run is a thread, and the tier sticks there.

### Starting a thread

**Starting a thread is a delegation whose result is delivered differently.** The assistant starts
one with a Home-only tool. The tool creates the child conversation and enqueues its first turn on
the existing delegation machinery, `delegation_runs` plus the task worker, which gives it durable
queueing and retries. This extends the run model rather than parameterising it. Today a run's rows
live in a subconversation of its source conversation, and completion wakes that source. A thread run
has a target conversation distinct from its source (Home), no subconversation, and a completion that
is recorded without a wake, because the user talks to the thread directly from then on. With no
subconversation, the run is a browser scope of its own rather than inheriting its source's tab.

**The first turn runs where the user can watch it, and the user can always join in.** On a surface
that streams turns live, the worker registers the thread's first turn there before claiming the run,
so the thread shows it as it happens and a message sent meanwhile steers it, like any turn in
progress. A message sent before the turn has started becomes the turn itself: the queued run is
retired in the same transaction as the message, and the user's turn answers the request already in
the thread. Either way there is never a competing turn.

**It is admitted exactly like a delegation.** Home's model picks the profile and writes the request,
so the target must accept it under the same `allowed_delegation_sources` check and the same
per-target `tools_policy` rules, and the request carries the same taint sources. Those rules match
the `delegation` tool tag and the `target_service_id` argument rather than a tool name, so the two
tools cannot drift apart. A user starting a thread from the client is a user choice, like picking a
profile today.

**User actions use the same path.** "Continue in a thread" and "New thread" go through the same
creation path, with a user-written opening message. "Move to thread…" moves a whole turn (the user
message with its tool rows and reply), never a single row. All of them pass the chokepoint described
under "Moving content across the boundary".

**When to thread is guidance in the prompt, not code.** The default profile's prompt describes when
a thread earns its place. The user's override is always one action away, so a wrong call is cheap.

Threads do not nest. A thread can delegate but cannot start threads.

### Moving content across the boundary

Four paths carry rows between conversations: an assistant-started seed, a user's "Continue in a
thread", a turn moved with "Move to thread…", and the echo copied into Home. **All four go through
one chokepoint**, so a path added later can't skip a rule. It applies three rules:

1. **Taint and profile.** Copied rows keep their taint and are re-stamped to the destination's
   profile, because the history window is scoped by profile and an unstamped row would be invisible
   there.
2. **The context gate.** Home turns, their tool output included, may only enter a profile that
   receives household context today. That is judged by the profile's *effective* context, the
   aggregated-context switch and its provider exclusions together, not by any single setting. Most
   acting or untrusted-reading profiles (`browser_profile`, `coder`, `media_analyst`) receive none,
   so they accept request text only. Any path that would carry Home turns into such a profile is
   refused. That means "Continue in a thread" with context, and "Move to thread…". The client offers
   only compatible destinations.
3. **Authorship.** Only text the user typed is stored as the user's words. A request the assistant
   wrote to open a thread is stored as machine-authored data, as delegation results already are, and
   the link record points at it so clients can show it as "Assistant asked: …". It therefore neither
   counts as user activity for memory review nor serves as evidence of what the user said, while
   still recording who owns the thread.

- **The seed follows the delegation boundary.** An assistant-started thread carries exactly what a
  delegation carries today: the request its starting turn wrote, plus any attachments it passed
  explicitly. Neither the anchor turn nor the Home transcript around it is copied, because the
  anchor may hold private tool output. That matters most for profiles that read untrusted content
  and can act, such as `browser_profile`, where a page could try to exfiltrate whatever the seed
  contains. The thread's "Started from" quote is drawn by the client from the link record. It is
  never part of the thread's model history. Only the user's own "Continue in a thread" copies the
  anchor and the Home turns leading up to it, as a deliberate choice to bring that context along,
  and only into a profile that already receives household context (rule 2 above). A seed is copied
  rather than read across conversations at every turn, so the thread's history doesn't depend on
  Home's later compaction or edits.
- **Home sees its threads as facts, not transcripts.** Home's assistant sees each live thread's
  title, state and, once it finishes, the echoed final message. That is enough to answer "how are
  the quotes going?" without merging histories. **Generated conversation summaries are never used
  for this.** They derive from possibly untrusted content and carry no taint, as
  [conversation-list-summaries.md](conversation-list-summaries.md) requires. The title Home sees is
  the one written when the thread was created, and it carries that turn's taint.
- **A thread does not see its sibling threads.**

### State

- **Working:** the thread has a queued or active turn, a pending delegation or a pending worker.
- **Failed:** the thread's last run or turn ended in a terminal failure, such as retries exhausted,
  a provider error or a crash. This is derived by code, so infrastructure failure is never presented
  as a request for input. The thread's assistant can also mark a job failed when the job itself
  can't be done.
- **Done:** the thread's assistant ends the job with a tool call, or the user marks it done.
- **Waiting on you:** anything else. The thread is not working, failed or done, so the next move is
  the user's. Every thread is always in exactly one state.
- **A done thread reopens when something new arrives in it.** That could be a reminder or callback
  it scheduled, a late delegation result, or a user message. The day the plumber comes is the same
  thread, not a new one. The thread goes back to working or waiting, its card and switcher row
  update, and finishing again writes a new echo.
- **Unread is a separate per-viewer count.** In a household, one person reading a thread does not
  clear another person's badge. Legacy conversations have no state.

**An assistant-finished job writes the echo.** When the thread's assistant ends the job, its final
message is copied into Home as a row attributed to the thread (see the rule above). When the user
marks a thread done, there may be no outcome to report: the last message could be a question or a
progress update. So nothing is echoed, and the card just shows that the user closed it. It pings
Home's stream and updates the card. The echo is a copy, not a model turn, so finishing a thread
never wakes Home's assistant. Delegations *inside* a thread wake the thread's profile in the
thread's conversation, as today.

### Telegram

Telegram assumes one chat is one conversation today, so three things change:

- **The turn slot becomes per conversation.** The slot is currently held per chat id, so a Home
  message sent while a thread turn runs would steer that thread's turn instead of starting a Home
  turn.
- **Delivery resolves a thread to its chat.** A thread's messages go to the chat with a reply to the
  thread's card, and the card is edited in place.
- **Replies to a card or a thread message route into the thread.** Other reply chains keep their
  current meaning, which is pulling the replied-to turn into context and adopting its profile. They
  do not become threads.

Native private-chat topics (Bot API 9.4) are a possible later step and not part of this design.

## Security

Nothing here changes what a profile can reach. A thread runs on a profile under that profile's tool
policy, exactly as a conversation does today. The new surface is what moves between conversations,
and each path is governed:

- **Starting a thread** is admitted by the target profile's delegation policy, and the request
  carries delegation taint.
- **Everything crossing a conversation boundary passes one chokepoint.** Copied rows keep their
  taint. A profile that doesn't receive household context today accepts request text only, and moves
  into it are refused. An assistant-started seed holds no more than a delegation carries, which is
  the request and its explicit attachments. Assistant-written text is never stored as the user's
  words, so it can't become memory evidence.
- **An echo exposes Home to no more than a delegation result does today.** A browser or research
  result is untrusted content. Today that result is written into the source conversation's history,
  and the default assistant is woken to restate it. The echo copies the same message with the same
  taint, and does not wake Home. Content steering a later Home turn is governed by the existing
  taint policy, as it is for delegation results now. Tightening that policy is a separate decision,
  and this design neither depends on it nor weakens it.
- **Home's view of a thread** is a title (carrying its creating turn's taint), a code-derived or
  explicitly set state, and echoes (carrying their source's taint). Generated summaries never reach
  a prompt.
- **The link record** joins two conversations owned by the same user. Creating a thread or moving a
  turn into one checks ownership of both ends.

## Deliberate simplifications

- **One Home per surface.** The Telegram Home and the web/iOS Home are separate conversations,
  because history is scoped by interface type. A household that uses both sees two Homes. Unifying
  them means lifting interface scoping from history, which is a separate project.

- **No nested threads.** One level covers the household use cases.

- **Automations from before Home stay where they are.** There is no migration. A wake created now
  has its destination written down, and one with none written down fires where it was created, so
  existing automations, reminders and script automations keep their behaviour. Moving one to Home is
  an edit by the user.

- **A Home-bound wake falls back to where it was created when Home no longer applies.** If threads
  are switched off after it was created, it fires in its source conversation, which is where it
  would have gone before Home existed.

- **An automation's "new" thread is an ordinary thread with nothing seeded.**

  - **Its title** is the automation's name, or the first line of its message when it has none (a
    one-off callback, a script's hand-off), plus the date. The title carries the wake's taint.
  - **Its first row** is the wake prompt, stored as every wake prompt is: a machine-authored
    internal row, never the user's words. The card therefore has no "Assistant asked" quote.
  - **Its profile** is the one the wake runs on, and later turns stay on it, because the thread's
    history is stamped with that profile. An event wake's thread stays on `event_handler`, whose
    confinement suits a thread holding untrusted event content.
  - **A profile that keeps no history keeps Home's in a thread.** `event_handler` keeps none so that
    unrelated events in a shared conversation do not mix; a thread holds one, so a reply there sees
    the event it answers.
  - **A wake that ends quietly leaves no thread.** It said nothing, so its thread and the wake's own
    rows are discarded rather than left as an empty card, unless the user has replied in the thread
    meanwhile, which makes it theirs and keeps it.
  - **Retries and push.** The thread's id is derived from the task, so a retry finds the thread its
    first attempt made. Push goes to the thread, as it would to any conversation the reply lands in.
  - **While the wake's turn runs, the card reads "waiting".** That turn runs in the task worker,
    outside the live stream that marks a thread working, and it is short.

- **Only the streaming turn endpoint absorbs a queued run.** A non-streaming message sent into a
  thread whose first turn hasn't started holds the conversation until it is answered; the first turn
  then runs after it rather than being absorbed. Clients use the streaming endpoint.

- **The seed is a snapshot.** A Home message edited after the thread started is not reflected in the
  thread.

- **Cards do not re-post.** A card that has scrolled out of view in Home stays where it is. "Waiting
  on you" is carried by the switcher badge, the sidebar and push notifications.

- **Tier choices don't stick in Home.** Home is everlasting, so a sticky tier would quietly apply to
  every later quick question.

- **Some profiles cannot finish their threads.** Closing a job is a tool call, so a profile with no
  Family Assistant tools (`coder`), or one that withholds every global grant, leaves its threads
  waiting until the user marks them done.

- **A working thread can't be marked done.** Work in progress outranks a close, so a thread marked
  done mid-turn would still read as working, and the turn's reply would reopen it. Both clients
  offer Mark done only once the thread is waiting or has failed.

- **Read marks follow the newest message on screen.** The web client marks a conversation read while
  its newest message is scrolled into view in a visible tab. When a turn sent from that tab ends, it
  marks through the conversation's newest message, because a streamed reply has no stored id on the
  client yet, so a message landing from elsewhere in that instant is marked read too.

- **A closing turn that never ends cleanly echoes nothing.** The echo is the message the closing
  turn ends with. A turn that ends without one, or is stopped after the close, leaves the thread
  closed with no echo; the next thing to arrive reopens it as usual.

- **A turn that closes the thread and then fails shows the thread as failed.** The user never got
  that turn's answer, so the failure is shown rather than hidden behind "done"; they can mark the
  thread done themselves.

- **Home reads facts for its recent threads only.** A status question is about live work, so Home's
  view covers the most recently active threads; older ones are in the switcher.

- **The web panel is a fixed proportion, and only wide windows get it.** There is no drag handle;
  the panel takes a fixed share of the width, and below a fixed width a thread opens in place of
  Home instead. Whatever is hidden behind a thread (Home behind an expanded or narrow-window thread,
  either one behind the phone's conversation list) keeps its conversation, live stream and draft,
  but not its scroll position: it comes back at its newest message.

- **A link straight to a thread lands in the centre first.** The client can't tell a thread from an
  ordinary conversation until Home's thread list arrives, so an address naming only the thread opens
  it in the centre and then moves it to the panel, with Home in the centre, once it is known.

- **Unread is counted for Home and threads only.** Legacy conversations have no state, so they have
  no read markers either.

- **iOS: routed requests and drafts on rare paths are correct, not seamless.** A prompt or shared
  file routed in while the app cannot yet tell whether Home exists is held and delivered once it
  can. A prompt routed into Home waits until Home is loaded and idle, showing a short "Will send
  when ready" note. Beyond that the app does not try to preserve everything. If several routes
  arrive while the Home lookup is failing, only the latest is delivered. Signing in again in place
  rebuilds the chat tab, even for the same account, so an unsent draft there is dropped. A draft
  written in the fallback conversation list moves into Home when Retry finds it, but nothing carries
  across a re-sign-in, which may be as someone else.

- **"Continue in a thread" carries a fixed window.** It copies the anchor's turn and at most four
  Home turns before it. That covers the exchange being continued and what led up to it; anything
  older stays in Home, and the user can say more in the opening message. The copies are stamped with
  the time they were made, so an old exchange the user chose doesn't age out of the thread's window
  before its first turn reads it.

- **Continuing into a profile without household context carries the request only.** With context it
  is refused, and the client offers "continue without context" there instead: a thread that starts
  from the user's opening message and keeps the anchor for its "Started from" quote, like an
  assistant-started seed.

- **A "Started from" quote can outlive its message.** The anchor is display-only. If the user later
  moves the exchange a thread was continued from, the thread keeps working and simply shows no
  quote; nothing retargets or snapshots it.

- **A moved turn is a copy, then a removal.** Its rows are copied into the thread, stamped with the
  time of the move so the turn is the thread's newest, and deleted from Home in the same
  transaction. Home keeps one marker row naming the thread, which clients render as "Moved to …" and
  which Home's assistant reads only as "an exchange was moved". A turn is moved only while neither
  conversation has a reply in progress, so its rows never land inside another turn. "In progress" is
  read from what is stored, not only from the live stream: a turn that has not ended with a reply or
  an error, including one waiting to resume after a restart, and a thread whose starting run has not
  finished, all count. Only an exchange the user started moves; a thread's echoed outcome stays in
  Home.

- **The web offers no working thread as a move destination.** The client reads "at rest" from the
  thread's state, so a thread working only on a background delegation, which the server would
  accept, is shown unavailable until it stops. Anything the client can't see, such as a turn running
  in another tab, is refused by the server and the client shows why.

- **The web quotes "Started from" from Home's loaded messages.** There is no endpoint for one
  message, so a thread continued from a message older than Home's loaded history, or from one moved
  since, says "Started from Home" without the quote.

- **iOS lists every done thread under "Done".** The switcher has no "Done today" cut-off. A done
  thread stays there until something reopens it, and older conversations stay under Earlier, so the
  switcher needs no second clock.

- **iOS marks read only what is on screen, a moment after it settles.** Home or a thread is marked
  read through its newest saved message once the reader has been at the bottom, with the app in the
  foreground, for about a second. A reply still streaming is marked when it is saved. A failed mark
  is retried only when a newer message arrives, never in a loop. "At the bottom" comes from the
  scroll view's own visibility report, which iOS 17 lacks. There it falls back to the bottom of the
  transcript having appeared, which can stay true after scrolling up within the loaded page.

- **iOS: overrides offer what the client can see, and the server decides.** "Move to thread…" is
  offered while Home looks at rest and lists threads as available from the state the client last
  loaded; a reply that started elsewhere a moment ago is refused by the server, and the refusal is
  shown. A "Started from" quote is drawn from Home's loaded messages, so a thread continued from a
  message Home no longer holds shows no quote. A move made on another device takes the moved rows
  out of Home when its marker arrives.

## Decisions

- **The assistant starts threads on its own from day one.** It doesn't propose them first. User
  overrides are one action away in both directions.
- **Done threads reopen** on later events, as described under State.
- **Card quick replies come only from explicit questions the thread asks.** A general
  suggested-replies mechanism may come later.

## Work plan

1. **Threads exist.** Add Home designation, the link record, the start-thread tool on delegation
   machinery, and seeding under the copy rule, behind a single global setting. Threads hang off
   Home, which is per user, not per profile.
   - Verified by functional tests: a started thread runs on its own profile.
   - The target's delegation policy is enforced.
   - Seeded rows are visible to the thread's history window and keep their taint.
   - Coder and browser threads receive their request and nothing else from Home: no transcript and
     no tool rows.
   - Moving a Home turn into a coder or browser thread is refused.
   - An assistant-written request is not a user row, and doesn't make memory review due.
   - An assistant-started seed holds only the request.
2. **Home on web.** In three steps.
   - The API (Home, the thread list and its derived state, thread metadata, the server-side profile
     pin), the live first turn and absorbing a queued run. Verified by API tests.
   - Home as the landing view, thread cards and the grouped sidebar, with a thread opening in place
     of Home behind a breadcrumb. Verified by Playwright tests of card state transitions and
     navigation.
   - The right-hand panel, once the chat view is split into a pane that can be shown twice.
3. **State and echo.** Add derived working, waiting and failed states, explicit done, per-viewer
   unread counts, the echo, and Home's view of thread facts. Verified by tests that Home answers a
   status question from thread facts, that an echo keeps its taint, that a thread ending on a
   question is waiting rather than done, and that a run that exhausts its retries is failed rather
   than waiting.
4. **Home on iOS.** Add the landing view, the switcher, cards and push navigation. Verified by unit
   tests on card state and UI tests on navigation.
5. **User overrides.** Add "Continue in a thread", "Move to thread…" (moves a turn), send-as-thread
   and "New thread" on both clients. Afterwards, an automation's "new conversation per run" becomes
   a thread off Home through the same thread-creation path.
6. **Telegram.** Add a per-conversation turn slot, delivery from thread to chat, edited-in-place
   cards, reply-to-card routing and echoes. Verified by tests that a Home message during a running
   thread turn starts its own turn rather than steering.
