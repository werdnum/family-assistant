import Foundation
import Observation

/// Home's threads and the ordinary conversations listed beside them, shared by
/// the thread cards in Home's transcript, the threads button's badge and the
/// thread switcher.
@MainActor
@Observable
final class HomeThreadsStore {
    let homeConversationID: String
    private(set) var threads: [ChatThreadSummary] = []
    private(set) var conversations: [ChatConversationSummary] = []
    private(set) var threadsRefreshFailed = false
    private(set) var conversationsRefreshFailed = false
    private(set) var isLoadingConversations = false
    /// Whether the ordinary conversations are on screen, so activity refreshes
    /// them too. Otherwise they are fetched when the switcher opens.
    @ObservationIgnored var listsConversations = false
    /// Opens a conversation over Home. Set by the view that owns navigation.
    @ObservationIgnored var openConversation: ((String) -> Void)?
    /// The conversation model pushed over Home, so a push hint reaches it.
    @ObservationIgnored weak var pushedModel: ChatViewModel?
    /// Home's own model: a turn moved out of it reloads it, and a thread
    /// continued from it quotes the message it started from.
    @ObservationIgnored weak var homeModel: ChatViewModel?
    /// The thread sheet Home shows, when a message or the composer asked for one.
    var presentedSheet: HomeThreadSheet?

    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private let errorReporter: ErrorReporter

    init(homeConversationID: String, apiClient: ChatAPIClient, errorReporter: ErrorReporter = .shared) {
        self.homeConversationID = homeConversationID
        self.apiClient = apiClient
        self.errorReporter = errorReporter
    }

    var waitingCount: Int {
        ThreadSwitcherSections.waitingCount(in: threads)
    }

    var workingThreads: [ChatThreadSummary] {
        threads.filter { $0.presentationState == .working }
    }

    var sections: ThreadSwitcherSections {
        ThreadSwitcherSections(
            threads: threads,
            conversations: conversations,
            homeConversationID: homeConversationID
        )
    }

    func thread(forSourceToolCallID toolCallID: String) -> ChatThreadSummary? {
        threads.first { $0.sourceToolCallID == toolCallID }
    }

    func thread(conversationID: String?) -> ChatThreadSummary? {
        guard let conversationID else {
            return nil
        }
        return threads.first { $0.conversationID == conversationID }
    }

    func open(_ conversationID: String) {
        openConversation?(conversationID)
    }

    func refreshThreads() async {
        do {
            threads = try await apiClient.listHomeThreads().threads
            threadsRefreshFailed = false
        } catch {
            guard !Self.isCancellation(error) else {
                return
            }
            threadsRefreshFailed = true
            errorReporter.report(error, component: "Chat.homeThreads")
        }
    }

    /// Threads a "Mark done" request is out for, so it is not sent twice.
    private(set) var markingDone: Set<String> = []
    /// A user action on a thread that failed, for Home's alert. Actions taken
    /// in a sheet show their failure in the sheet instead.
    var actionFailure: ThreadActionFailure?

    /// Close a thread as done, as the user. The card and row show the closed
    /// thread straight from the server's answer, and the list is refreshed for
    /// anything else that changed. A failure is reported, never dropped.
    func markDone(_ conversationID: String) async {
        guard !markingDone.contains(conversationID) else {
            return
        }
        markingDone.insert(conversationID)
        defer { markingDone.remove(conversationID) }
        do {
            let closed = try await apiClient.markThreadDone(conversationID: conversationID)
            replace(closed)
        } catch {
            guard !Self.isCancellation(error) else {
                return
            }
            actionFailure = ThreadActionFailure(
                title: "Couldn’t Update Thread",
                message: "Couldn’t mark this thread done. \(error.localizedDescription)"
            )
            errorReporter.report(error, component: "Chat.markThreadDone")
            return
        }
        await refreshThreads()
    }

    /// Start a thread of Home. The thread is listed at once from the server's
    /// answer, and the list refreshed for anything else that changed. A
    /// refusal is reported and returned with the server's reason, never
    /// dropped; nil means the attempt was cancelled.
    func startThread(_ start: HomeThreadStart) async -> Result<ChatThreadSummary, ThreadActionFailure>? {
        let started: ChatStartThreadResult
        do {
            started = try await apiClient.startHomeThread(start)
        } catch {
            guard !Self.isCancellation(error) else {
                return nil
            }
            errorReporter.report(error, component: "Chat.startThread")
            return .failure(ThreadActionFailure(title: "Couldn’t Start Thread", message: error.localizedDescription))
        }
        insertOrReplace(started.thread)
        await refreshThreads()
        return .success(started.thread)
    }

    /// Send-as-thread's start: on success the new thread is opened, and a
    /// failure goes to Home's alert, since no sheet is showing by then.
    func startThreadAndOpen(_ start: HomeThreadStart) async -> Bool {
        switch await startThread(start) {
        case let .success(thread):
            open(thread.conversationID)
            return true
        case let .failure(failure):
            actionFailure = failure
            return false
        case nil:
            return false
        }
    }

    /// Move the Home turn `messageID` belongs to into `conversationID`. Home
    /// is reloaded so its marker replaces the turn, and the thread's row is
    /// updated from the server's answer.
    func moveTurn(messageID: Int, to conversationID: String) async -> Result<ChatMoveTurnResult, ThreadActionFailure>? {
        let moved: ChatMoveTurnResult
        do {
            moved = try await apiClient.moveTurnToThread(conversationID: conversationID, messageID: messageID)
        } catch {
            guard !Self.isCancellation(error) else {
                return nil
            }
            errorReporter.report(error, component: "Chat.moveTurnToThread")
            return .failure(ThreadActionFailure(title: "Couldn’t Move to Thread", message: error.localizedDescription))
        }
        insertOrReplace(moved.thread)
        await homeModel?.reloadMessages()
        await refreshThreads()
        return .success(moved)
    }

    /// The text of the Home message a thread was continued from, while Home
    /// still holds it.
    func anchorQuote(for thread: ChatThreadSummary) -> String? {
        guard thread.homeConversationID == homeConversationID, let anchor = thread.anchorMessageID else {
            return nil
        }
        return homeModel?.shownText(ofPersistedMessage: anchor)
    }

    private func insertOrReplace(_ thread: ChatThreadSummary) {
        if let index = threads.firstIndex(where: { $0.conversationID == thread.conversationID }) {
            threads[index] = thread
        } else {
            threads.insert(thread, at: 0)
        }
    }

    /// Show the caller's unread count for a thread they have just read.
    func applyUnreadCount(_ unreadCount: Int, for conversationID: String) {
        guard let index = threads.firstIndex(where: { $0.conversationID == conversationID }),
              threads[index].unreadCount != unreadCount
        else {
            return
        }
        threads[index].unreadCount = unreadCount
    }

    private func replace(_ thread: ChatThreadSummary) {
        guard let index = threads.firstIndex(where: { $0.conversationID == thread.conversationID }) else {
            return
        }
        threads[index] = thread
    }

    func refreshConversations() async {
        isLoadingConversations = true
        defer { isLoadingConversations = false }
        do {
            conversations = try await apiClient.listOrdinaryConversations()
            conversationsRefreshFailed = false
        } catch {
            guard !Self.isCancellation(error) else {
                return
            }
            conversationsRefreshFailed = true
            errorReporter.report(error, component: "Chat.ordinaryConversations")
        }
    }

    /// Refresh on an activity ping: a thread run starting or ending, or any
    /// conversation of the caller's changing.
    func refreshForActivity() async {
        await refreshThreads()
        if listsConversations {
            await refreshConversations()
        }
    }

    func searchConversations(matching query: String) async throws -> [ChatConversationSummary] {
        do {
            return try await apiClient.searchConversations(query: query)
        } catch {
            if !Self.isCancellation(error) {
                errorReporter.report(error, component: "Chat.conversationSearch")
            }
            throw error
        }
    }

    private static func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || (error as? URLError)?.code == .cancelled
    }
}

/// A user action on threads that failed: what the alert reporting it says.
struct ThreadActionFailure: Error, Equatable, Identifiable {
    let title: String
    let message: String

    var id: String { title + message }
}

/// A sheet Home shows for starting a thread or moving a turn into one.
enum HomeThreadSheet: Identifiable, Equatable {
    /// Start a thread from scratch.
    case newThread
    /// Send-as-thread: pick the profile the composer's message opens a thread on.
    case sendAsThread
    /// Continue in a thread from the Home message `anchorMessageID`.
    case continueFrom(anchorMessageID: Int, quote: String)
    /// Move the Home turn holding `messageID` into a thread.
    case move(messageID: Int, quote: String)

    var id: String {
        switch self {
        case .newThread: "new-thread"
        case .sendAsThread: "send-as-thread"
        case let .continueFrom(anchor, _): "continue-\(anchor)"
        case let .move(messageID, _): "move-\(messageID)"
        }
    }
}

/// Whether this server gives the caller a Home, remembered between launches so
/// the chat tab opens straight onto the right screen. It is refreshed on every
/// launch; a Home that has gone away or changed is corrected in place.
enum HomeAvailability {
    enum State: Equatable {
        case unknown
        case disabled
        case enabled(homeConversationID: String)
        /// The lookup failed, which says nothing about whether threads are on.
        /// Never remembered: the next lookup decides.
        case failed
    }

    static let serverKey = "chat.home.server"
    static let conversationKey = "chat.home.conversationID"

    static func cached(serverURL: String?, defaults: UserDefaults = .standard) -> State {
        guard let serverURL, defaults.string(forKey: serverKey) == serverURL,
              let stored = defaults.string(forKey: conversationKey)
        else {
            return .unknown
        }
        return stored.isEmpty ? .disabled : .enabled(homeConversationID: stored)
    }

    static func state(for response: ChatHomeResponse) -> State {
        guard response.enabled, let conversationID = response.conversationID, !conversationID.isEmpty else {
            return .disabled
        }
        return .enabled(homeConversationID: conversationID)
    }

    static func store(_ state: State, serverURL: String?, defaults: UserDefaults = .standard) {
        guard let serverURL else {
            return
        }
        switch state {
        case .unknown:
            clear(defaults: defaults)
            return
        case .failed:
            return
        case .disabled:
            defaults.set("", forKey: conversationKey)
        case let .enabled(homeConversationID):
            defaults.set(homeConversationID, forKey: conversationKey)
        }
        defaults.set(serverURL, forKey: serverKey)
    }

    static func clear(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: serverKey)
        defaults.removeObject(forKey: conversationKey)
    }

    /// What to show once a fresh answer arrives, given what is showing now.
    /// Home that has gone or moved is replaced at once, since its routes no
    /// longer answer for it. A Home that appears while the conversation list is
    /// showing waits for the next launch rather than pulling the screen away
    /// from someone mid-chat. After a failed lookup the answer is taken as it
    /// comes: the user asked for it with Retry, or signed in again.
    static func resolved(current: State, fetched: State) -> State {
        switch current {
        case .unknown, .enabled, .failed:
            return fetched
        case .disabled:
            return .disabled
        }
    }

    /// What to show when a lookup fails. A launch with nothing remembered shows
    /// the failure (with the conversation list, so chat stays usable) rather
    /// than reading it as threads being off; anything already showing stays.
    static func afterLookupFailure(current: State) -> State {
        current == .unknown ? .failed : current
    }
}

/// Looks up the caller's Home for the chat tab, and looks again on request
/// after a failure.
@MainActor
@Observable
final class HomeDiscovery {
    private(set) var state: HomeAvailability.State
    @ObservationIgnored private let serverURL: String?
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let lookUp: @MainActor () async throws -> ChatHomeResponse
    @ObservationIgnored private let reportFailure: @MainActor (Error) -> Void

    init(
        serverURL: String?,
        defaults: UserDefaults = .standard,
        lookUp: @escaping @MainActor () async throws -> ChatHomeResponse,
        reportFailure: @escaping @MainActor (Error) -> Void
    ) {
        self.serverURL = serverURL
        self.defaults = defaults
        self.lookUp = lookUp
        self.reportFailure = reportFailure
        state = HomeAvailability.cached(serverURL: serverURL, defaults: defaults)
    }

    func refresh() async {
        do {
            let fetched = HomeAvailability.state(for: try await lookUp())
            HomeAvailability.store(fetched, serverURL: serverURL, defaults: defaults)
            state = HomeAvailability.resolved(current: state, fetched: fetched)
        } catch {
            reportFailure(error)
            state = HomeAvailability.afterLookupFailure(current: state)
        }
    }

    /// The route a root may act on in `state`. Until the lookup has settled
    /// whether there is a Home, a route is held (an empty route stands in), so
    /// a prompt or shared file is not consumed by a provisional conversation
    /// list that Home then replaces. The root that wins receives it.
    static func deliverableRoute(_ route: ChatRoute, state: HomeAvailability.State) -> ChatRoute {
        switch state {
        case .enabled, .disabled:
            return route
        case .unknown, .failed:
            return ChatRoute()
        }
    }

    /// Forget what is showing and look Home up afresh, as after signing in
    /// again, which may be as someone else. The remembered Home is ignored: it
    /// may be the previous account's.
    func restart() async {
        state = .unknown
        await refresh()
    }

    /// Look again, if the last lookup failed.
    func retryIfFailed() async {
        guard state == .failed else {
            return
        }
        await refresh()
    }
}

/// What a chat route asks of the Home screen.
enum HomeRouteAction: Equatable {
    /// Show Home itself, popping anything opened over it.
    case showHome
    /// Open a conversation over Home.
    case open(conversationID: String)
    /// Show Home with the keyboard up, ready for a new message. "New chat"
    /// from outside the app means this: a new conversation is Home's next
    /// message, or a thread the user starts from there.
    case composeInHome
    /// Send a prompt in Home, the way quick questions go.
    case sendInHome(prompt: String)

    static func resolve(_ route: ChatRoute, homeConversationID: String) -> HomeRouteAction {
        // A share hand-off lands in Home's composer: quick capture lives there.
        if route.sharedAttachmentBatchID != nil {
            return .showHome
        }
        if let prompt = route.initialPrompt, !prompt.isEmpty {
            return .sendInHome(prompt: prompt)
        }
        if route.newConversationRequestID != nil {
            return .composeInHome
        }
        if let conversationID = route.conversationID, conversationID != homeConversationID {
            return .open(conversationID: conversationID)
        }
        return .showHome
    }
}
