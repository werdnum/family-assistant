import SwiftUI
import UIKit

enum SubconversationPeekLoadState: Equatable {
    case loading
    case ready
    case unavailable
    case failed(String)
}

@MainActor
@Observable
final class SubconversationPeekViewModel {
    private static let terminalStatuses: Set<String> = ["completed", "failed"]
    private static let pollableIdleStatuses: Set<String> = ["queued", "awaiting_remote", "awaiting_children"]
    private static let pageLimit = 50
    private static let textFlushInterval: Duration = .milliseconds(50)
    private static let idlePollInterval: Duration = .seconds(15)
    private static let maxReconnectAttempts = 5

    let conversationID: String
    let subconversationID: String
    let modelTierLabels: [String: String]

    private(set) var messages: [ChatMessage] = []
    private(set) var delegation: ChatPendingDelegation?
    private(set) var delegationError: String?
    private(set) var requestText: String?
    private(set) var childDelegations: [ChatPendingDelegation] = []
    private(set) var loadState: SubconversationPeekLoadState = .loading
    private(set) var hasMoreBefore = false
    private(set) var isLoadingEarlier = false
    private(set) var isStreaming = false
    private(set) var isListening = false
    var attachmentErrorMessage: String?
    let browserLiveView: BrowserLiveViewController

    @ObservationIgnored private let authManager: AuthManager
    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private let errorReporter: ErrorReporter
    @ObservationIgnored private let reconnectBaseDelaySeconds: Double
    @ObservationIgnored private var persistedRows: [ChatBackendMessage] = []
    @ObservationIgnored private var activeTurns: [ChatActiveTurnInfo] = []
    @ObservationIgnored private var replayingTurnID: String?
    @ObservationIgnored private var liveMessage: ChatMessage?
    @ObservationIgnored private var pendingText = ""
    @ObservationIgnored private var needsSnapshotReconcile = false
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var runGeneration = 0

    init(
        conversationID: String,
        subconversationID: String,
        seedDelegation: ChatPendingDelegation?,
        authManager: AuthManager,
        apiClient: ChatAPIClient,
        modelTierLabels: [String: String] = [:],
        errorReporter: ErrorReporter = .shared,
        reconnectBaseDelaySeconds: Double = 1.0
    ) {
        self.conversationID = conversationID
        self.subconversationID = subconversationID
        self.delegation = seedDelegation
        self.requestText = seedDelegation?.requestPreview
        self.authManager = authManager
        self.apiClient = apiClient
        self.modelTierLabels = modelTierLabels
        self.errorReporter = errorReporter
        self.reconnectBaseDelaySeconds = reconnectBaseDelaySeconds
        self.browserLiveView = BrowserLiveViewController { path in
            try await apiClient.downloadAttachment(path: path).0
        }
    }

    deinit {
        streamTask?.cancel()
        flushTask?.cancel()
        pollTask?.cancel()
    }

    var displayName: String {
        delegation?.profileDisplayName ?? "Subconversation"
    }

    var statusLabel: String {
        guard let delegation else {
            return isStreaming ? "Working on it" : "Live view"
        }
        switch delegation.status {
        case "completed":
            return "Completed"
        case "failed":
            return "Failed"
        default:
            return delegation.statusLabel
        }
    }

    var isTerminal: Bool {
        guard let status = delegation?.status else { return false }
        return Self.terminalStatuses.contains(status) && !isStreaming
    }

    var isFailed: Bool {
        delegation?.status == "failed"
    }

    var isBrowserProfile: Bool {
        guard let profileID = delegation?.targetProfileID else { return false }
        return profileID.contains("browser")
    }

    func start() {
        stop()
        runGeneration += 1
        let generation = runGeneration
        loadState = persistedRows.isEmpty ? .loading : .ready
        streamTask = Task { [weak self] in
            await self?.runLifecycle(generation: generation)
        }
    }

    func stop() {
        runGeneration += 1
        streamTask?.cancel()
        streamTask = nil
        flushTask?.cancel()
        flushTask = nil
        pollTask?.cancel()
        pollTask = nil
        isListening = false
    }

    func refresh() async {
        do {
            let snapshot = try await apiClient.getSubconversationMessages(
                conversationID: conversationID,
                subconversationID: subconversationID,
                limit: Self.pageLimit
            )
            apply(snapshot: snapshot)
            if !isListening && shouldStayConnected(snapshot: snapshot) {
                start()
            }
        } catch let ChatAPIError.server(statusCode, _, _) where statusCode == 404 {
            loadState = .unavailable
        } catch {
            if persistedRows.isEmpty {
                loadState = .failed(error.localizedDescription)
            }
        }
    }

    func loadEarlierMessages() async {
        guard hasMoreBefore, !isLoadingEarlier,
              let oldestIDString = persistedRows.first?.internalID,
              let oldestID = Int(oldestIDString)
        else {
            return
        }
        isLoadingEarlier = true
        defer { isLoadingEarlier = false }
        do {
            let page = try await apiClient.getSubconversationMessages(
                conversationID: conversationID,
                subconversationID: subconversationID,
                beforeID: oldestID,
                limit: Self.pageLimit
            )
            let existingIDs = Set(persistedRows.map(\.internalID))
            let fresh = page.messages.filter { !existingIDs.contains($0.internalID) }
            persistedRows = fresh + persistedRows
            hasMoreBefore = page.hasMoreBefore
            rebuildMessages()
        } catch {
            errorReporter.report(error, component: "SubconversationPeek.loadEarlier")
        }
    }

    private func shouldStayConnected(snapshot: ChatSubconversationMessagesResponse) -> Bool {
        let hasRunning = snapshot.activeTurns.contains { $0.status == "running" }
        let terminal = snapshot.delegation.map { Self.terminalStatuses.contains($0.status) } ?? false
        return hasRunning || !terminal
    }

    private func runLifecycle(generation: Int) async {
        let snapshot: ChatSubconversationMessagesResponse
        do {
            snapshot = try await apiClient.getSubconversationMessages(
                conversationID: conversationID,
                subconversationID: subconversationID,
                limit: Self.pageLimit
            )
        } catch let ChatAPIError.server(statusCode, _, _) where statusCode == 404 {
            guard generation == runGeneration else { return }
            loadState = .unavailable
            return
        } catch {
            guard generation == runGeneration, !Task.isCancelled else { return }
            loadState = .failed(error.localizedDescription)
            errorReporter.report(error, component: "SubconversationPeek.load")
            return
        }

        guard generation == runGeneration, !Task.isCancelled else { return }
        apply(snapshot: snapshot)

        let runningTurn = snapshot.activeTurns.first { $0.status == "running" }
        if !shouldStayConnected(snapshot: snapshot) {
            isListening = false
            return
        }

        var nextFromSeq = runningTurn.map { $0.firstSeq ?? 0 } ?? -1
        if let runningTurn {
            replayingTurnID = runningTurn.turnID
            startLiveBubble(turnID: runningTurn.turnID, startedAt: runningTurn.startedAt)
            rebuildMessages()
        }

        isListening = true
        needsSnapshotReconcile = false
        updateIdlePollTask(generation: generation)
        var attempt = 0

        while generation == runGeneration, !Task.isCancelled, attempt <= Self.maxReconnectAttempts {
            do {
                if needsSnapshotReconcile {
                    let reconciled = try await apiClient.getSubconversationMessages(
                        conversationID: conversationID,
                        subconversationID: subconversationID,
                        limit: Self.pageLimit
                    )
                    guard generation == runGeneration, !Task.isCancelled else { return }
                    needsSnapshotReconcile = false
                    clearLiveBubble()
                    replayingTurnID = nil
                    apply(snapshot: reconciled)
                    updateIdlePollTask(generation: generation)
                    if !shouldStayConnected(snapshot: reconciled) {
                        break
                    }
                    attempt = 0
                }

                let stream = try await apiClient.streamSubconversation(
                    conversationID: conversationID,
                    subconversationID: subconversationID,
                    fromSeq: nextFromSeq,
                    follow: true
                )

                var receivedRealEvent = false
                var stopAfterStream = false

                for try await event in stream {
                    guard generation == runGeneration, !Task.isCancelled else {
                        return
                    }
                    if let seq = event.seq {
                        nextFromSeq = seq + 1
                    }
                    if event.type != .heartbeat && event.type != .streamDropped {
                        receivedRealEvent = true
                    }
                    let shouldContinue = await handle(event: event, generation: generation)
                    if !shouldContinue {
                        stopAfterStream = true
                        break
                    }
                }

                if receivedRealEvent && !needsSnapshotReconcile {
                    attempt = 0
                }
                guard generation == runGeneration, !Task.isCancelled else { return }

                if stopAfterStream && !needsSnapshotReconcile && isTerminal {
                    break
                }

                if needsSnapshotReconcile {
                    attempt += 1
                    if attempt > Self.maxReconnectAttempts {
                        break
                    }
                    let delaySeconds = min(reconnectBaseDelaySeconds * pow(2.0, Double(attempt - 1)), 10.0)
                    try? await Task.sleep(for: .seconds(delaySeconds))
                    continue
                }

                // Reconcile state after stream EOF or stream_dropped before deciding to reconnect.
                do {
                    let refreshed = try await apiClient.getSubconversationMessages(
                        conversationID: conversationID,
                        subconversationID: subconversationID,
                        limit: Self.pageLimit
                    )
                    guard generation == runGeneration, !Task.isCancelled else { return }
                    apply(snapshot: refreshed)
                    if !shouldStayConnected(snapshot: refreshed) {
                        clearLiveBubble()
                        replayingTurnID = nil
                        rebuildMessages()
                        break
                    }
                } catch {
                    needsSnapshotReconcile = true
                }

                attempt += 1
                if attempt > Self.maxReconnectAttempts {
                    break
                }
                let delaySeconds = min(reconnectBaseDelaySeconds * pow(2.0, Double(attempt - 1)), 10.0)
                try? await Task.sleep(for: .seconds(delaySeconds))
            } catch let ChatAPIError.server(statusCode, _, _) where statusCode == 410 {
                guard generation == runGeneration, !Task.isCancelled else { return }
                // Ring buffer rotated past requested seq: reload full persisted
                // history (keeping all persisted rows) and switch to live tailing.
                replayingTurnID = nil
                if let refreshed = try? await apiClient.getSubconversationMessages(
                    conversationID: conversationID,
                    subconversationID: subconversationID,
                    limit: Self.pageLimit
                ) {
                    guard generation == runGeneration, !Task.isCancelled else { return }
                    apply(snapshot: refreshed)
                    if !shouldStayConnected(snapshot: refreshed) {
                        clearLiveBubble()
                        rebuildMessages()
                        break
                    }
                } else {
                    needsSnapshotReconcile = true
                }
                nextFromSeq = -1
                attempt = 0
            } catch {
                guard generation == runGeneration, !Task.isCancelled else { return }
                attempt += 1
                if attempt > Self.maxReconnectAttempts {
                    break
                }
                let delaySeconds = min(reconnectBaseDelaySeconds * pow(2.0, Double(attempt - 1)), 10.0)
                try? await Task.sleep(for: .seconds(delaySeconds))
            }
        }

        if generation == runGeneration {
            isListening = false
            pollTask?.cancel()
            pollTask = nil
        }
    }

    private func handle(event: ChatStreamEvent, generation: Int) async -> Bool {
        switch event.type {
        case .heartbeat, .connected, .userInput:
            return true
        case .browserFrame:
            if let frame = event.browserFrame {
                browserLiveView.apply(frame: frame)
            }
            return true
        case .streamDropped:
            return false
        case .turnStarted:
            let turnID = event.turnID ?? UUID().uuidString
            replayingTurnID = turnID
            startLiveBubble(turnID: turnID, startedAt: Date())
            rebuildMessages()
            updateIdlePollTask(generation: generation)
            return true
        case .text:
            guard let delta = event.text, !delta.isEmpty else {
                if var live = liveMessage, live.isLoading {
                    live.isLoading = false
                    liveMessage = live
                    rebuildMessages()
                }
                return true
            }
            ensureLiveBubble(turnID: event.turnID)
            pendingText += delta
            scheduleTextFlush(generation: generation)
            return true
        case .toolCall:
            if let toolCall = event.toolCall {
                ensureLiveBubble(turnID: event.turnID)
                flushPendingTextNow()
                if var live = liveMessage {
                    if !live.toolCalls.contains(where: { $0.id == toolCall.id }) {
                        let offset = live.text.utf16.count
                        live.toolCalls.append(
                            ChatToolCall(
                                id: toolCall.id,
                                name: toolCall.displayName,
                                argumentsText: toolCall.argumentsText,
                                resultText: nil,
                                attachments: [],
                                status: .running,
                                display: toolCall.display,
                                textOffset: offset
                            )
                        )
                    }
                    live.isLoading = false
                    liveMessage = live
                    rebuildMessages()
                }
            }
            return true
        case .toolResult:
            if var live = liveMessage, let callID = event.toolCallID,
               let idx = live.toolCalls.firstIndex(where: { $0.id == callID }) {
                live.toolCalls[idx].resultText = event.toolResult
                if !event.attachments.isEmpty {
                    live.toolCalls[idx].attachments.append(contentsOf: event.attachments)
                }
                live.toolCalls[idx].status = event.toolOutcome ?? .succeeded
                liveMessage = live
                rebuildMessages()
            }
            return true
        case .attachment:
            if event.attachmentSource == .response {
                ensureLiveBubble(turnID: event.turnID)
                if var live = liveMessage {
                    let existingIDs = Set(live.attachments.map(\.id))
                    let fresh = event.attachments.filter { !existingIDs.contains($0.id) }
                    if !fresh.isEmpty {
                        live.attachments.append(contentsOf: fresh)
                    }
                    live.isLoading = false
                    liveMessage = live
                    rebuildMessages()
                }
            }
            return true
        case .toolConfirmationRequest:
            if var live = liveMessage, let callID = event.confirmation?.toolCallID,
               let idx = live.toolCalls.firstIndex(where: { $0.id == callID }) {
                live.toolCalls[idx].status = .awaitingApproval
                liveMessage = live
                rebuildMessages()
            }
            return true
        case .toolConfirmationResult:
            return true
        case .error:
            ensureLiveBubble(turnID: event.turnID)
            flushPendingTextNow()
            if var live = liveMessage {
                let errText = event.errorMessage ?? "An error occurred."
                live.text = live.text.isEmpty ? errText : "\(live.text)\n\n\(errText)"
                live.isLoading = false
                liveMessage = live
                rebuildMessages()
            }
            return true
        case .turnEnded:
            flushPendingTextNow()
            do {
                let refreshed = try await apiClient.getSubconversationMessages(
                    conversationID: conversationID,
                    subconversationID: subconversationID,
                    limit: Self.pageLimit
                )
                guard generation == runGeneration else { return false }
                clearLiveBubble()
                replayingTurnID = nil
                apply(snapshot: refreshed)
                updateIdlePollTask(generation: generation)
                return shouldStayConnected(snapshot: refreshed)
            } catch {
                // Force bounded snapshot reconciliation instead of staying idle
                // on a healthy follow stream with stale running state.
                if var live = liveMessage {
                    live.isLoading = false
                    live.status = .complete
                    liveMessage = live
                    rebuildMessages()
                }
                isStreaming = false
                needsSnapshotReconcile = true
                return false
            }
        case .message:
            do {
                let refreshed = try await apiClient.getSubconversationMessages(
                    conversationID: conversationID,
                    subconversationID: subconversationID,
                    limit: Self.pageLimit
                )
                guard generation == runGeneration else { return false }
                apply(snapshot: refreshed)
                updateIdlePollTask(generation: generation)
                return shouldStayConnected(snapshot: refreshed)
            } catch {
                needsSnapshotReconcile = true
                return false
            }
        }
    }

    private func apply(snapshot: ChatSubconversationMessagesResponse) {
        persistedRows = snapshot.messages
        activeTurns = snapshot.activeTurns
        hasMoreBefore = snapshot.hasMoreBefore
        if let updated = snapshot.delegation {
            delegation = updated
        }
        delegationError = snapshot.delegationError
        if let text = snapshot.requestText, !text.isEmpty {
            requestText = text
        }
        childDelegations = snapshot.childDelegations
        if let frame = snapshot.browserView {
            browserLiveView.apply(frame: frame)
        }
        loadState = .ready
        rebuildMessages()
    }

    private func startLiveBubble(turnID: String, startedAt: Date) {
        flushTask?.cancel()
        flushTask = nil
        pendingText = ""
        liveMessage = ChatMessage(
            id: "sub_stream_\(turnID)",
            role: .assistant,
            text: "",
            createdAt: startedAt,
            toolCalls: [],
            attachments: [],
            isLoading: true,
            status: .running,
            processingProfileID: delegation?.targetProfileID,
            errorTraceback: nil,
            turnID: turnID
        )
        isStreaming = true
    }

    private func ensureLiveBubble(turnID: String?) {
        if liveMessage == nil {
            startLiveBubble(turnID: turnID ?? UUID().uuidString, startedAt: Date())
        }
    }

    private func clearLiveBubble() {
        flushTask?.cancel()
        flushTask = nil
        pendingText = ""
        liveMessage = nil
        isStreaming = false
    }

    private func scheduleTextFlush(generation: Int) {
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            try? await Task.sleep(for: Self.textFlushInterval)
            guard let self, generation == self.runGeneration, !Task.isCancelled else { return }
            self.flushTask = nil
            self.flushPendingTextNow()
        }
    }

    private func flushPendingTextNow() {
        flushTask?.cancel()
        flushTask = nil
        guard !pendingText.isEmpty, var live = liveMessage else { return }
        live.text += pendingText
        live.isLoading = false
        pendingText = ""
        liveMessage = live
        rebuildMessages()
    }

    private func updateIdlePollTask(generation: Int) {
        let shouldPoll = liveMessage == nil
            && delegation.map { Self.pollableIdleStatuses.contains($0.status) } ?? false
        if !shouldPoll {
            pollTask?.cancel()
            pollTask = nil
            return
        }
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while let self, generation == self.runGeneration, !Task.isCancelled {
                try? await Task.sleep(for: Self.idlePollInterval)
                guard generation == self.runGeneration, !Task.isCancelled else { return }
                if let refreshed = try? await self.apiClient.getSubconversationMessages(
                    conversationID: self.conversationID,
                    subconversationID: self.subconversationID,
                    limit: Self.pageLimit
                ) {
                    guard generation == self.runGeneration else { return }
                    self.apply(snapshot: refreshed)
                    if !self.shouldStayConnected(snapshot: refreshed) {
                        self.pollTask = nil
                        return
                    }
                }
            }
        }
    }

    private func rebuildMessages() {
        let filteredRows: [ChatBackendMessage]
        if let replayingTurnID {
            filteredRows = persistedRows.filter { row in
                row.role == .user || row.turnID != replayingTurnID
            }
        } else {
            filteredRows = persistedRows
        }

        var runningIDs = ChatViewModel.runningTurnIDs(in: activeTurns)
        if let liveTurnID = liveMessage?.turnID {
            runningIDs.insert(liveTurnID)
        }

        var rendered = ChatViewModel.groupToolCallTurns(
            ChatViewModel.renderMessages(from: filteredRows, runningTurnIDs: runningIDs)
        )
        if let liveMessage {
            rendered.append(liveMessage)
        }
        messages = rendered
    }
}

extension SubconversationPeekViewModel: ChatAttachmentLoading {
    var attachmentCacheScope: String {
        guard let server = authManager.validatedServerURL()?.absoluteString else {
            return ""
        }
        return "\(server)#\(authManager.authEpoch)"
    }

    func authenticatedImageData(for attachment: ChatAttachment) async throws -> Data {
        guard let contentURL = attachment.contentURL else {
            throw ChatAPIError.validation("Attachment does not have an image URL.")
        }
        return try await apiClient.downloadAttachment(path: contentURL).0
    }

    func downloadAttachmentForSharing(_ attachment: ChatAttachment) async -> URL? {
        do {
            guard let contentURL = attachment.contentURL else {
                throw ChatAPIError.validation("Attachment does not have a download URL.")
            }
            let (data, _) = try await apiClient.downloadAttachment(path: contentURL)
            let filename = URL(fileURLWithPath: attachment.name).lastPathComponent
            let destination = FileManager.default.temporaryDirectory
                .appendingPathComponent(filename.isEmpty ? "Attachment" : filename)
            try data.write(to: destination, options: .atomic)
            return destination
        } catch {
            attachmentErrorMessage = "Could not download attachment. \(error.localizedDescription)"
            errorReporter.report(error, component: "SubconversationPeek.downloadAttachment")
            return nil
        }
    }
}

/// Modal read-only live view into a delegated subconversation.
struct SubconversationPeekSheetView: View {
    let conversationID: String
    let rootDelegation: ChatPendingDelegation
    let authManager: AuthManager
    let apiClient: ChatAPIClient
    let modelTierLabels: [String: String]
    let onClose: () -> Void

    var body: some View {
        NavigationStack {
            SubconversationPeekScreen(
                conversationID: conversationID,
                delegation: rootDelegation,
                authManager: authManager,
                apiClient: apiClient,
                modelTierLabels: modelTierLabels,
                onClose: onClose
            )
            .navigationDestination(for: ChatPendingDelegation.self) { child in
                SubconversationPeekScreen(
                    conversationID: conversationID,
                    delegation: child,
                    authManager: authManager,
                    apiClient: apiClient,
                    modelTierLabels: modelTierLabels,
                    onClose: onClose
                )
            }
        }
        .environment(\.homeThreads, nil)
    }
}

private struct SubconversationPeekScreen: View {
    let conversationID: String
    let delegation: ChatPendingDelegation
    let authManager: AuthManager
    let apiClient: ChatAPIClient
    let modelTierLabels: [String: String]
    let onClose: () -> Void

    @Environment(\.scenePhase) private var scenePhase
    @State private var viewModel: SubconversationPeekViewModel

    init(
        conversationID: String,
        delegation: ChatPendingDelegation,
        authManager: AuthManager,
        apiClient: ChatAPIClient,
        modelTierLabels: [String: String],
        onClose: @escaping () -> Void
    ) {
        self.conversationID = conversationID
        self.delegation = delegation
        self.authManager = authManager
        self.apiClient = apiClient
        self.modelTierLabels = modelTierLabels
        self.onClose = onClose
        _viewModel = State(
            initialValue: SubconversationPeekViewModel(
                conversationID: conversationID,
                subconversationID: delegation.subconversationID ?? "",
                seedDelegation: delegation,
                authManager: authManager,
                apiClient: apiClient,
                modelTierLabels: modelTierLabels
            )
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            statusHeaderBar

            BrowserLiveViewCard(
                controller: viewModel.browserLiveView,
                isRunning: !viewModel.isTerminal,
                isBrowserProfile: viewModel.isBrowserProfile,
                compact: true
            )

            Group {
                switch viewModel.loadState {
                case .loading:
                    ProgressView("Loading subconversation…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .ready:
                    transcriptArea
                case .unavailable:
                    ContentUnavailableView(
                        "Subconversation unavailable",
                        systemImage: "eye.slash",
                        description: Text("This delegated run could not be found.")
                    )
                case .failed(let message):
                    ContentUnavailableView {
                        Label("Couldn’t load subconversation", systemImage: "wifi.exclamationmark")
                    } description: {
                        Text(message)
                    } actions: {
                        Button("Try again") {
                            viewModel.start()
                        }
                    }
                }
            }

            if !viewModel.childDelegations.isEmpty {
                childDelegationsTray
            }

            if let errorText = viewModel.delegationError, !errorText.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                    Text(errorText)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.08))
                .accessibilityIdentifier("subconversation-peek-error")
            }

            Divider()
            readOnlyFooterBar
        }
        .navigationTitle(viewModel.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Done", action: onClose)
                    .accessibilityIdentifier("subconversation-peek-close")
            }
        }
        .onAppear {
            viewModel.start()
        }
        .onDisappear {
            viewModel.stop()
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .background {
                viewModel.stop()
            } else if newPhase == .active && !viewModel.isTerminal {
                viewModel.start()
            }
        }
        .alert(
            "Attachment Error",
            isPresented: Binding(
                get: { viewModel.attachmentErrorMessage != nil },
                set: { if !$0 { viewModel.attachmentErrorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(viewModel.attachmentErrorMessage ?? "")
        }
        .accessibilityIdentifier("subconversation-peek-view")
    }

    private var statusHeaderBar: some View {
        HStack(spacing: 8) {
            Label("Read only", systemImage: "lock.fill")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("subconversation-peek-read-only")

            Spacer()

            HStack(spacing: 5) {
                if viewModel.isStreaming || !viewModel.isTerminal {
                    ProgressView()
                        .controlSize(.mini)
                } else if viewModel.isFailed {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(.red)
                        .font(.caption)
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.caption)
                }
                Text(viewModel.statusLabel)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(viewModel.isFailed ? .red : .secondary)
            }
            .accessibilityIdentifier("subconversation-peek-status")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.secondary.opacity(0.08))
    }

    private var transcriptArea: some View {
        StickyBottomScroll(
            followTrigger: followTrigger,
            canFollow: { UIApplication.shared.applicationState == .active }
        ) {
            VStack(spacing: 14) {
                if viewModel.hasMoreBefore {
                    Button {
                        Task { await viewModel.loadEarlierMessages() }
                    } label: {
                        if viewModel.isLoadingEarlier {
                            ProgressView()
                                .controlSize(.small)
                        } else {
                            Text("Load earlier messages")
                        }
                    }
                    .font(.subheadline)
                    .disabled(viewModel.isLoadingEarlier)
                    .padding(.vertical, 4)
                    .accessibilityIdentifier("subconversation-peek-load-earlier")
                }

                if viewModel.messages.isEmpty {
                    emptyWaitingView
                } else {
                    ForEach(viewModel.messages) { message in
                        MessageBubble(
                            message: message,
                            attachmentLoader: viewModel,
                            userRoleTitle: "Delegated request",
                            modelTierLabels: viewModel.modelTierLabels
                        )
                        .id(message.id)
                        .accessibilityIdentifier("subconversation-peek-message-\(message.id)")
                    }
                }
            }
            .padding()
        }
        .refreshable {
            await viewModel.refresh()
        }
    }

    private var followTrigger: String? {
        guard let last = viewModel.messages.last else { return nil }
        return "\(last.id)#\(last.text.count)#\(last.toolCalls.count)#\(last.toolCalls.filter { $0.resultText != nil }.count)"
    }

    private var emptyWaitingView: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let requestText = viewModel.requestText, !requestText.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Delegated request")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(requestText)
                        .font(.subheadline)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
            }

            HStack(spacing: 8) {
                if !viewModel.isTerminal {
                    ProgressView()
                        .controlSize(.small)
                    Text(
                        viewModel.delegation?.status == "awaiting_remote"
                            ? "\(viewModel.displayName) is running remotely…"
                            : "Waiting for \(viewModel.displayName) to begin…"
                    )
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                } else {
                    Text("No messages recorded in this subconversation.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.top, 16)
        .accessibilityIdentifier("subconversation-peek-empty")
    }

    private var childDelegationsTray: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            VStack(alignment: .leading, spacing: 4) {
                ForEach(viewModel.childDelegations) { child in
                    if child.subconversationID != nil {
                        NavigationLink(value: child) {
                            HStack(spacing: 6) {
                                ProgressView()
                                    .controlSize(.mini)
                                Text(
                                    "\(Text(child.profileDisplayName).fontWeight(.medium)) · \(child.statusLabel) · \(child.startedAgo(now: context.date))"
                                )
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                Spacer(minLength: 4)
                                Image(systemName: "chevron.right")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("subconversation-peek-child-delegation")
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color.secondary.opacity(0.08))
    }

    private var readOnlyFooterBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "lock.fill")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("Read-only view")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            if viewModel.isStreaming {
                Label("Streaming live", systemImage: "dot.radiowaves.left.and.right")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Color.accentColor)
            } else if viewModel.isListening {
                Text("Listening for updates…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if viewModel.isFailed {
                Text("Failed")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.red)
            } else if viewModel.isTerminal {
                Text("Completed")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.green)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color(.systemBackground))
        .accessibilityIdentifier("subconversation-peek-footer")
    }
}
