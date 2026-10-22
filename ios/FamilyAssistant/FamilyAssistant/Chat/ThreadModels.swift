import Foundation

/// The caller's Home (`GET /api/v1/chat/home`). With threads disabled there is
/// no Home and the app keeps its conversation list.
struct ChatHomeResponse: Decodable, Equatable {
    let enabled: Bool
    let conversationID: String?

    enum CodingKeys: String, CodingKey {
        case enabled
        case conversationID = "conversation_id"
    }
}

/// Where a thread stands, as its card and switcher row show it. A state this
/// client does not know yet decodes as `.unknown` rather than failing the list.
enum ChatThreadState: Equatable, Decodable {
    case working
    case waiting
    case failed
    case done
    case unknown(String)

    init(rawValue: String) {
        switch rawValue {
        case "working": self = .working
        case "waiting": self = .waiting
        case "failed": self = .failed
        case "done": self = .done
        default: self = .unknown(rawValue)
        }
    }

    init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }
}

/// How a thread's state is presented: the one mapping the card, the switcher's
/// grouping and the threads button's badge all use, so they cannot disagree.
/// A state this client does not know is presented as waiting, the server's own
/// catch-all, so a thread that may need the user is never hidden.
enum ThreadPresentationState: Equatable {
    case waiting
    case working
    case failed
    case done

    init(_ state: ChatThreadState) {
        switch state {
        case .working: self = .working
        case .failed: self = .failed
        case .done: self = .done
        case .waiting, .unknown: self = .waiting
        }
    }

    /// What a card says about a thread; nil is a thread not loaded yet.
    static func label(for state: Self?) -> String {
        switch state {
        case .waiting: "Waiting on you"
        case .working: "Working"
        case .failed: "Failed"
        case .done: "Done"
        case nil: "Starting"
        }
    }
}

/// Who closed a thread, while that close is what its state shows.
enum ChatThreadCloser: Equatable {
    case assistant
    case user

    init?(rawValue: String?) {
        switch rawValue {
        case "assistant": self = .assistant
        case "user": self = .user
        default: return nil
        }
    }
}

/// One thread of a Home: what its card and switcher row show.
struct ChatThreadSummary: Equatable, Identifiable {
    let conversationID: String
    let homeConversationID: String
    let title: String
    let profileID: String
    let state: ChatThreadState
    let sourceToolCallID: String?
    let seedRequest: String?
    let preview: String?
    let createdAt: Date
    let updatedAt: Date
    var closedBy: ChatThreadCloser? = nil
    var closedAt: Date? = nil
    var echoMessageID: Int? = nil
    /// The caller's own unread count: someone else reading it does not clear it.
    var unreadCount = 0
    /// The Home message the user continued the thread from, for its "Started
    /// from" quote. Display only: the message may since have left Home.
    var anchorMessageID: Int? = nil

    var id: String { conversationID }

    var presentationState: ThreadPresentationState {
        ThreadPresentationState(state)
    }

    /// The line under the title: the thread's latest message, or what it was
    /// asked to do before it has said anything.
    var previewLine: String? {
        for candidate in [preview, seedRequest] {
            if let candidate, !candidate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return candidate
            }
        }
        return nil
    }

    static let userClosedLine = "You marked this done"

    /// What a card or switcher row says under the title. A thread the user
    /// closed says so: its latest message may be a question or a progress
    /// update rather than an outcome. Anything else shows its latest message,
    /// which for a thread its assistant finished is the outcome.
    var detailLine: String? {
        if presentationState == .done, closedBy == .user {
            return Self.userClosedLine
        }
        return previewLine
    }

    /// Whether "Mark done" is offered: only once the thread is waiting or has
    /// failed. Work in progress outranks a close, so a working thread marked
    /// done would still read as working and its turn's reply would reopen it.
    var canMarkDone: Bool {
        presentationState == .waiting || presentationState == .failed
    }

    /// The card and row label for VoiceOver, which reads them as one element.
    var accessibilitySummary: String {
        var parts = [title, ThreadPresentationState.label(for: presentationState)]
        if let detailLine {
            parts.append(detailLine)
        }
        if unreadCount > 0 {
            parts.append("\(unreadCount) unread")
        }
        return parts.joined(separator: ", ")
    }
}

extension ChatThreadSummary: Decodable {
    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case homeConversationID = "home_conversation_id"
        case title
        case profileID = "profile_id"
        case state
        case sourceToolCallID = "source_tool_call_id"
        case seedRequest = "seed_request"
        case preview
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case closedBy = "closed_by"
        case closedAt = "closed_at"
        case echoMessageID = "echo_message_id"
        case unreadCount = "unread_count"
        case anchorMessageID = "anchor_message_id"
    }

    // In an extension so the memberwise initializer stays available.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        conversationID = try container.decode(String.self, forKey: .conversationID)
        homeConversationID = try container.decode(String.self, forKey: .homeConversationID)
        title = try container.decode(String.self, forKey: .title)
        profileID = try container.decode(String.self, forKey: .profileID)
        state = try container.decode(ChatThreadState.self, forKey: .state)
        sourceToolCallID = try container.decodeIfPresent(String.self, forKey: .sourceToolCallID)
        seedRequest = try container.decodeIfPresent(String.self, forKey: .seedRequest)
        preview = try container.decodeIfPresent(String.self, forKey: .preview)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
        closedBy = ChatThreadCloser(rawValue: try container.decodeIfPresent(String.self, forKey: .closedBy))
        closedAt = try container.decodeIfPresent(Date.self, forKey: .closedAt)
        echoMessageID = try container.decodeIfPresent(Int.self, forKey: .echoMessageID)
        unreadCount = try container.decodeIfPresent(Int.self, forKey: .unreadCount) ?? 0
        anchorMessageID = try container.decodeIfPresent(Int.self, forKey: .anchorMessageID)
    }
}

/// The caller's unread count once a read is recorded
/// (`POST /api/v1/chat/conversations/{id}/read`).
struct ChatMarkReadResponse: Decodable, Equatable {
    let conversationID: String
    let unreadCount: Int

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case unreadCount = "unread_count"
    }
}

struct ChatTurnFeedbackRequest: Encodable {
    let rating: ChatTurnRating
    let comment: String?
}

struct ChatMarkReadRequest: Encodable {
    let throughMessageID: Int

    enum CodingKeys: String, CodingKey {
        case throughMessageID = "through_message_id"
    }
}

/// Whether a conversation's read marker should move, and how far. A marker
/// moves only for Home and threads (ordinary conversations have none), only
/// while the reader can see the newest message — the conversation on screen,
/// the app in the foreground, scrolled to the bottom — and only forward.
enum ReadMarkerGate {
    struct Inputs: Equatable {
        var kind: ChatConversationKind?
        var isOnScreen: Bool
        var isSceneActive: Bool
        var isAtLatest: Bool
        var latestPersistedMessageID: Int?
        var markedThrough: Int?
    }

    /// Whether watching for a position to mark is worth anything right now.
    static func isReading(_ inputs: Inputs) -> Bool {
        (inputs.kind == .home || inputs.kind == .thread)
            && inputs.isOnScreen && inputs.isSceneActive && inputs.isAtLatest
    }

    /// The message to mark read through, or nil to leave the marker where it is.
    static func messageToMark(_ inputs: Inputs) -> Int? {
        guard isReading(inputs), let latest = inputs.latestPersistedMessageID else {
            return nil
        }
        if let marked = inputs.markedThrough, marked >= latest {
            return nil
        }
        return latest
    }

    /// The id the server knows a rendered message by, for a persisted row.
    static func persistedID(of messageID: String) -> Int? {
        guard messageID.hasPrefix("msg_") else {
            return nil
        }
        return Int(messageID.dropFirst(4))
    }
}

struct ChatHomeThreadsResponse: Decodable, Equatable {
    let homeConversationID: String?
    let threads: [ChatThreadSummary]

    enum CodingKeys: String, CodingKey {
        case homeConversationID = "home_conversation_id"
        case threads
    }
}

/// Whether a conversation is the caller's Home, one of its threads, or an
/// ordinary conversation.
enum ChatConversationKind: Equatable {
    case home
    case thread
    case conversation

    init?(rawValue: String?) {
        switch rawValue {
        case "home": self = .home
        case "thread": self = .thread
        case "conversation": self = .conversation
        default: return nil
        }
    }
}

/// The name of the Home-only tool the assistant starts a thread with. Its call
/// id is the started thread's `source_tool_call_id`.
enum ChatThreadTool {
    static let startThreadName = "start_thread"

    /// The title the assistant gave a thread, read from the call's arguments,
    /// for a card whose thread has not reached the thread list yet.
    static func requestedTitle(from argumentsText: String) -> String? {
        guard let data = argumentsText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let title = object["title"] as? String
        else {
            return nil
        }
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// A tool group's calls, with those that started a thread taken out to be shown
/// as the thread's card. A call becomes a card when its thread is known, or when
/// it has not failed (a thread that is starting has not reached the list yet).
/// A refused call stays an ordinary tool row: there is no thread to open.
struct ThreadCardSplit {
    let threadCalls: [ChatToolCall]
    let otherCalls: [ChatToolCall]

    init(
        toolCalls: [ChatToolCall],
        showsCards: Bool,
        hasThread: (_ toolCallID: String) -> Bool = { _ in false }
    ) {
        guard showsCards else {
            threadCalls = []
            otherCalls = toolCalls
            return
        }
        var cards: [ChatToolCall] = []
        var others: [ChatToolCall] = []
        for call in toolCalls {
            if Self.isCard(call, hasThread: hasThread) {
                cards.append(call)
            } else {
                others.append(call)
            }
        }
        threadCalls = cards
        otherCalls = others
    }

    private static func isCard(_ call: ChatToolCall, hasThread: (String) -> Bool) -> Bool {
        guard call.name == ChatThreadTool.startThreadName else {
            return false
        }
        if hasThread(call.id) {
            return true
        }
        guard call.status.isPending || call.status == .succeeded else {
            return false
        }
        return !(call.resultText?.hasPrefix("Error:") ?? false)
    }
}

/// The chat switcher's contents: Home, then threads grouped by what they
/// need, then the ordinary conversations under "Earlier". Every done thread is
/// listed under Done until something reopens it.
struct ThreadSwitcherSections: Equatable {
    struct Group: Equatable, Identifiable {
        let title: String
        let threads: [ChatThreadSummary]

        var id: String { title }
    }

    let groups: [Group]
    let earlier: [ChatConversationSummary]

    static let waitingTitle = ThreadPresentationState.label(for: .waiting)
    static let workingTitle = ThreadPresentationState.label(for: .working)
    static let failedTitle = ThreadPresentationState.label(for: .failed)
    static let doneTitle = ThreadPresentationState.label(for: .done)

    /// Threads keep the server's order (newest first) within their group,
    /// grouped by `ThreadPresentationState`. Earlier holds only conversations
    /// that are neither Home nor one of its threads.
    init(
        threads: [ChatThreadSummary],
        conversations: [ChatConversationSummary],
        homeConversationID: String?
    ) {
        var waiting: [ChatThreadSummary] = []
        var working: [ChatThreadSummary] = []
        var failed: [ChatThreadSummary] = []
        var done: [ChatThreadSummary] = []
        for thread in threads {
            switch thread.presentationState {
            case .working: working.append(thread)
            case .failed: failed.append(thread)
            case .done: done.append(thread)
            case .waiting: waiting.append(thread)
            }
        }
        groups = [
            Group(title: Self.waitingTitle, threads: waiting),
            Group(title: Self.workingTitle, threads: working),
            Group(title: Self.failedTitle, threads: failed),
            Group(title: Self.doneTitle, threads: done),
        ].filter { !$0.threads.isEmpty }
        let excluded = Set(threads.map(\.conversationID)).union([homeConversationID].compactMap { $0 })
        earlier = conversations.filter { conversation in
            !excluded.contains(conversation.conversationID)
                && (conversation.kind == nil || conversation.kind == "conversation")
        }
    }

    /// Threads waiting on the user, which the threads button badges.
    static func waitingCount(in threads: [ChatThreadSummary]) -> Int {
        threads.filter { $0.presentationState == .waiting }.count
    }
}

/// A thread "Move to thread…" lists, with why it cannot take the turn now, if
/// it can't. Unavailable threads are listed disabled rather than hidden, so the
/// user sees why the thread they had in mind is not offered.
struct ThreadMoveDestination: Identifiable, Equatable {
    let thread: ChatThreadSummary
    let unavailableReason: String?

    var id: String { thread.conversationID }
    var isAvailable: Bool { unavailableReason == nil }

    /// Every thread of Home, those that can take a Home turn first, each group
    /// in the server's order (newest first). A thread takes one only when its
    /// profile receives household context (the context gate: anything else
    /// the server refuses) and it is at rest, since a turn lands as the
    /// thread's newest and must not interleave with a reply in progress.
    /// `householdContextProfileIDs` is nil while the profile list is unknown,
    /// which leaves nothing to offer rather than guessing.
    static func list(
        threads: [ChatThreadSummary],
        householdContextProfileIDs: Set<String>?
    ) -> [ThreadMoveDestination] {
        let destinations = threads.map { thread in
            ThreadMoveDestination(
                thread: thread,
                unavailableReason: unavailableReason(
                    for: thread,
                    householdContextProfileIDs: householdContextProfileIDs
                )
            )
        }
        return destinations.filter(\.isAvailable) + destinations.filter { !$0.isAvailable }
    }

    private static func unavailableReason(
        for thread: ChatThreadSummary,
        householdContextProfileIDs: Set<String>?
    ) -> String? {
        guard let householdContextProfileIDs else {
            return "Couldn’t check which profiles take Home conversations."
        }
        guard householdContextProfileIDs.contains(thread.profileID) else {
            return "Runs on \(thread.profileID), which doesn’t take Home conversations."
        }
        if thread.presentationState == .working {
            return "Working. Try again once it has finished."
        }
        return nil
    }
}

/// What a Home message's context menu offers for threads. Only a saved
/// message of a turn is a candidate: it names the turn to the server, and a
/// "Moved to …" marker is a pointer, not a turn.
struct HomeTurnThreadActions: Equatable {
    /// The id the server knows the message by.
    let messageID: Int
    /// "Continue in a thread": once the message's turn has finished.
    let canContinue: Bool
    /// "Move to thread…": only while Home is at rest (no reply in progress,
    /// here or elsewhere), and only for an exchange the user started. A
    /// thread's echoed outcome and a proactive message stay in Home.
    let canMove: Bool

    /// The turns among `messages` that the user started.
    static func userStartedTurnIDs(in messages: [ChatMessage]) -> Set<String> {
        Set(messages.lazy.filter { $0.role == .user && $0.movedToThread == nil }.compactMap(\.turnID))
    }

    static func resolve(
        for message: ChatMessage,
        userStartedTurnIDs: Set<String>,
        homeIsAtRest: Bool,
        runningTurnIDs: Set<String>
    ) -> HomeTurnThreadActions? {
        guard message.movedToThread == nil,
              let turnID = message.turnID,
              let messageID = ReadMarkerGate.persistedID(of: message.id)
        else {
            return nil
        }
        let turnHasEnded = !runningTurnIDs.contains(turnID)
            && message.status != .running && !message.isLoading
        let userStarted = message.echoedFromThread == nil && userStartedTurnIDs.contains(turnID)
        let actions = HomeTurnThreadActions(
            messageID: messageID,
            canContinue: turnHasEnded,
            canMove: turnHasEnded && homeIsAtRest && userStarted
        )
        return actions.canContinue || actions.canMove ? actions : nil
    }
}

/// The choices "New thread" and "Continue in a thread" offer, given the
/// profile picked. Context (the anchor's exchange and the Home turns before
/// it) is offered only into a profile that takes Home conversations; anywhere
/// else the thread starts from the opening message alone, which is then
/// required.
struct ThreadStartOptions: Equatable {
    let anchorMessageID: Int?
    let profileID: String
    /// Nil while the profile list is unknown, which offers no context.
    let householdContextProfileIDs: Set<String>?

    var offersContext: Bool {
        anchorMessageID != nil && (householdContextProfileIDs?.contains(profileID) ?? false)
    }

    func carriesContext(includeContext: Bool) -> Bool {
        offersContext && includeContext
    }

    func canStart(prompt: String, includeContext: Bool) -> Bool {
        carriesContext(includeContext: includeContext)
            || !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func start(turnID: String, prompt: String, includeContext: Bool) -> HomeThreadStart {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return HomeThreadStart(
            turnID: turnID,
            profileID: profileID,
            prompt: trimmed.isEmpty ? nil : trimmed,
            anchorMessageID: anchorMessageID,
            includeContext: anchorMessageID == nil || carriesContext(includeContext: includeContext)
        )
    }
}

/// What a thread is started with. Whoever offers the start keeps one for the
/// attempt, so a retry sends the same `turnID`: the server then returns the
/// thread a timed-out attempt already started instead of starting a second.
struct HomeThreadStart: Equatable {
    let turnID: String
    var profileID: String
    var prompt: String?
    var anchorMessageID: Int? = nil
    var includeContext = true
    var modelTier: String? = nil
    var attachments: [ChatAttachment] = []

    /// This start as a retry of `previous`: under its turn id when nothing
    /// else changed, so the server treats it as the same start. A start whose
    /// choices changed is a new start, or the server would answer it with
    /// the thread the earlier attempt may already have made.
    func retrying(_ previous: HomeThreadStart?) -> HomeThreadStart {
        guard let previous else {
            return self
        }
        let same = HomeThreadStart(
            turnID: previous.turnID,
            profileID: profileID,
            prompt: prompt,
            anchorMessageID: anchorMessageID,
            includeContext: includeContext,
            modelTier: modelTier,
            attachments: attachments
        )
        return same == previous ? same : self
    }
}
