import Foundation

/// The chat operations whose failures are routed through ``ChatErrorClassifier``.
///
/// Classification is keyed on **operation × error × user-initiated**, not error
/// type alone: the same transport failure is silent when a background resync
/// refreshes the list but shows inline feedback when the user pulled to refresh.
/// The operation also decides whether a status code is meaningful — a 404 on a
/// per-conversation read means the conversation was deleted, while a 404 on the
/// account-global list is just a transport failure.
enum ChatOperation: String {
    /// Full conversation-list refetch (`listConversations`). Advisory when driven
    /// by resync; user-initiated on pull-to-refresh and bootstrap.
    case conversationsRefresh = "conversations_refresh"
    /// Recent-page conversation summary refresh (`listRecentConversations`).
    case recentConversationsRefresh = "recent_conversations_refresh"
    /// Incremental catch-up merge of newly persisted messages (`mergeNewMessages`).
    case messagesMerge = "messages_merge"
    /// Full message-history load for a conversation (`loadMessages`).
    case messagesLoad = "messages_load"
    /// Processing-profile list load (`loadProfiles`).
    case profilesLoad = "profiles_load"
    /// The ~15s pending-approvals poll (`loadPendingConfirmations`). Prod cluster G.
    case pendingApprovalsPoll = "pending_approvals_poll"
    /// A user send / steer submission failing at the point of action.
    case sendTurn = "send_turn"
    /// A user stop request failing.
    case stopTurn = "stop_turn"
    /// A user tool-confirmation submission failing.
    case confirmTool = "confirm_tool"
    /// A user attachment operation (import / remove / download) failing.
    case attachmentOp = "attachment_op"

    /// Advisory reads are background reconciliation: their failures are never
    /// modal. A persistent run of them drives the coordinator's degraded health
    /// instead, so silent-forever failure is impossible.
    var isAdvisoryRead: Bool {
        switch self {
        case .conversationsRefresh,
             .recentConversationsRefresh,
             .messagesMerge,
             .messagesLoad,
             .profilesLoad,
             .pendingApprovalsPoll:
            true
        case .sendTurn, .stopTurn, .confirmTool, .attachmentOp:
            false
        }
    }

    /// Whether the operation reads a single named conversation, so a 403/404 is a
    /// per-conversation access/existence signal rather than a transport failure.
    var isPerConversationRead: Bool {
        switch self {
        case .messagesMerge, .messagesLoad:
            true
        case .conversationsRefresh,
             .recentConversationsRefresh,
             .profilesLoad,
             .pendingApprovalsPoll,
             .sendTurn,
             .stopTurn,
             .confirmTool,
             .attachmentOp:
            false
        }
    }
}

/// The inputs the classifier keys on. The error is optional so a clean EOF (the
/// follow stream finishing without throwing) is expressible; `midTurn`
/// disambiguates an expected post-`turn_ended` EOF from a suspicious mid-turn one.
struct ChatErrorContext {
    let operation: ChatOperation
    let error: Error?
    /// Whether the user directly initiated the operation (send, stop, confirm,
    /// attachment op, pull-to-refresh, bootstrap). Advisory resync/push refreshes
    /// pass `false`.
    let userInitiated: Bool
    /// A clean EOF landing mid-turn is suspicious (drop/resume territory); the same
    /// EOF after `turn_ended` is the expected idle close. Only meaningful for the
    /// clean-EOF (`error == nil`) case.
    let midTurn: Bool
    /// The server's `Retry-After`, parsed by the caller when the response carried
    /// it. Only consulted for a 429 verdict.
    let retryAfter: TimeInterval?

    init(
        operation: ChatOperation,
        error: Error?,
        userInitiated: Bool,
        midTurn: Bool = false,
        retryAfter: TimeInterval? = nil
    ) {
        self.operation = operation
        self.error = error
        self.userInitiated = userInitiated
        self.midTurn = midTurn
        self.retryAfter = retryAfter
    }
}

/// The surfacing verdict for a classified failure. Distinct from the raw error:
/// two identical transport errors on the same operation can resolve to different
/// surfaces depending on `userInitiated`.
enum ChatErrorSurface: Equatable {
    /// No UI; only the existing per-operation breadcrumb. Advisory failures that
    /// have not yet crossed the degraded threshold.
    case silent
    /// No modal; feed the coordinator's degraded advisory-health input. A run of
    /// advisory failures crosses this after N in a row (recovery clears it).
    case degradedHealth
    /// Inline feedback at the point of action (a bubble affordance / thread text),
    /// tagged for the `Chat.inlineErrorPresented` breadcrumb.
    case inlineFeedback(reason: InlineReason)
    /// The shared "Chat Error" modal (`Chat.alertPresented`). Truly unrecoverable
    /// or a user-initiated read failure that keeps its existing modal UX.
    case modal
    /// Route to the existing re-auth affordance (`AuthManager.authRequired`); never
    /// a modal.
    case authFlow
    /// The conversation was deleted (404): remove it from the list / show a
    /// gone-state rather than a transport error.
    case conversationGone
    /// Honor `Retry-After`: schedule one retry for an advisory read after the
    /// given delay (nil ⇒ a default), or tell a user action to try again later.
    case retryAfter(TimeInterval?)

    /// The inline surfaces, each tagged onto the `Chat.inlineErrorPresented`
    /// breadcrumb so inline popup rates are measurable alongside modal rates.
    enum InlineReason: String {
        /// A user-initiated read (pull-to-refresh) failed transiently.
        case userReadFailed = "user_read_failed"
        /// A 403 on a conversation: access changed. Inline on the thread, not
        /// re-auth, not a generic modal.
        case accessChanged = "access_changed"
        /// A 429 on a user action: tell them to try again later.
        case rateLimited = "rate_limited"
        /// A user action (send/stop/confirm/attachment) failed and surfaces at the
        /// point of action (slice 2 builds the retry bubble).
        case actionFailed = "action_failed"
    }
}

/// Central, pure, table-driven error taxonomy (§4.5). Replaces scattered
/// per-site `errorMessage = error.localizedDescription` decisions with one
/// classifier keyed on operation × error × user-initiated, so the surfacing
/// policy is stated once and unit-testable in isolation.
enum ChatErrorClassifier {
    static func classify(_ context: ChatErrorContext) -> ChatErrorSurface {
        // Auth is endpoint-independent and terminal: a rejected refresh already
        // latched `authRequired`, which drives the dedicated presentation. Never a
        // modal, regardless of operation or who initiated it.
        if isAuthTerminal(context.error) {
            return .authFlow
        }

        if let statusCode = serverStatusCode(context.error) {
            return classifyStatus(statusCode, context: context)
        }

        // A clean EOF (the follow stream closed without throwing). Expected after a
        // turn ends (idle proxy / server shutdown) — silent; suspicious mid-turn —
        // hand to the drop/resume machinery, which is silent to the user too.
        if context.error == nil {
            return context.operation.isAdvisoryRead ? advisorySurface(context) : .silent
        }

        // Transport errors (URLError timeouts, connection loss) and everything else
        // fall to the operation's default: advisory reads degrade, user actions
        // surface inline, and a user-initiated read keeps its existing modal/inline
        // behavior.
        return defaultSurface(context)
    }

    // MARK: - Status codes

    private static func classifyStatus(
        _ statusCode: Int,
        context: ChatErrorContext
    ) -> ChatErrorSurface {
        switch statusCode {
        case 401:
            return .authFlow
        case 403 where context.operation.isPerConversationRead:
            // Access to this conversation changed. Inline on the thread — NOT
            // re-auth (the session is still valid), NOT a generic modal.
            return .inlineFeedback(reason: .accessChanged)
        case 404 where context.operation.isPerConversationRead:
            // The conversation was deleted server-side. Treat as gone, not a
            // transport error: remove it from the list / show a gone-state.
            return .conversationGone
        case 429:
            // A BACKGROUND advisory read schedules one silent retry after
            // `Retry-After`. A user-initiated read (pull-to-refresh) or a user action
            // is told to try again later inline — never the modal path
            // `handleUserReadFailure` maps `.retryAfter` onto.
            return context.operation.isAdvisoryRead && !context.userInitiated
                ? .retryAfter(context.retryAfter)
                : .inlineFeedback(reason: .rateLimited)
        default:
            // 5xx and other 4xx fall to the operation default; a sustained run of
            // advisory 5xx crosses the degraded threshold via the caller's counter.
            return defaultSurface(context)
        }
    }

    // MARK: - Defaults

    private static func advisorySurface(_ context: ChatErrorContext) -> ChatErrorSurface {
        // A single advisory failure is silent; the caller's consecutive-failure
        // counter promotes it to `degradedHealth` once the threshold is crossed.
        // The classifier states the ceiling (never modal); the caller supplies the
        // count, so the classifier stays pure.
        .silent
    }

    private static func defaultSurface(_ context: ChatErrorContext) -> ChatErrorSurface {
        if context.operation.isAdvisoryRead {
            // A user-initiated read (pull-to-refresh, bootstrap) keeps its existing
            // surface; a background advisory read is silent (→ degraded via count).
            return context.userInitiated
                ? .inlineFeedback(reason: .userReadFailed)
                : .silent
        }
        return .inlineFeedback(reason: .actionFailed)
    }

    // MARK: - Error inspection

    /// The HTTP status of a `ChatAPIError.server`, or nil for non-HTTP errors.
    static func serverStatusCode(_ error: Error?) -> Int? {
        guard let error, case ChatAPIError.server(let statusCode, _, _) = error else {
            return nil
        }
        return statusCode
    }

    /// Whether the error is the terminal auth failure that already latched
    /// `authRequired`. A response-time 401 surfaces as `AuthError.noCredentials`
    /// after a rejected refresh.
    static func isAuthTerminal(_ error: Error?) -> Bool {
        guard let error else {
            return false
        }
        if case AuthError.noCredentials = error {
            return true
        }
        if case AuthError.authRejected = error {
            return true
        }
        return false
    }
}
