import Observation
import SwiftUI

enum SharedConversationLoadState: Equatable {
    case loading
    case ready
    case unavailable
    case authenticationRequired
    case failed(String)
}

enum ConversationShareStatus: Equatable {
    case unavailable
    case loading
    case inactive
    case active
    case failed
}

extension ConversationShareTarget {
    /// A Home message that can be linked to on its own: a saved, finished user
    /// or assistant message with something to read. A marker Home draws as a
    /// thread is not one.
    static func homeMessage(_ message: ChatMessage, in conversationID: String) -> ConversationShareTarget? {
        guard message.role == .user || message.role == .assistant,
              message.movedToThread == nil,
              message.startedThread == nil,
              message.status != .running, !message.isLoading,
              !message.text.isEmpty,
              let messageID = ReadMarkerGate.persistedID(of: message.id)
        else {
            return nil
        }
        return .message(conversationID: conversationID, messageID: messageID)
    }
}

/// Owner-side sharing state. The server intentionally returns only whether an
/// active link exists, never the active bearer token, so sharing an already-shared
/// conversation rotates the link before presenting it.
@MainActor
@Observable
final class ConversationShareViewModel {
    private(set) var status: ConversationShareStatus = .unavailable
    private(set) var isUpdating = false
    var actionErrorMessage: String?

    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private let errorReporter: ErrorReporter
    @ObservationIgnored private var target: ConversationShareTarget?
    @ObservationIgnored private var operationGeneration = 0

    init(apiClient: ChatAPIClient, errorReporter: ErrorReporter = .shared) {
        self.apiClient = apiClient
        self.errorReporter = errorReporter
    }

    func loadStatus(target: ConversationShareTarget?) async {
        operationGeneration += 1
        let generation = operationGeneration
        self.target = target
        isUpdating = false
        actionErrorMessage = nil
        guard let target else {
            status = .unavailable
            return
        }
        status = .loading
        do {
            let active = try await apiClient.getShareStatus(target: target)
            guard generation == operationGeneration, self.target == target else { return }
            status = active ? .active : .inactive
        } catch {
            guard generation == operationGeneration, self.target == target else { return }
            status = .failed
            errorReporter.report(error, component: "ConversationShare.status")
        }
    }

    func createShare(target: ConversationShareTarget) async -> URL? {
        let previousStatus = status
        let generation = beginMutation(target: target)
        do {
            let url = try await apiClient.createShare(target: target)
            guard finishMutation(generation: generation, target: target) else { return nil }
            status = .active
            return url
        } catch AuthError.authRejected, AuthError.noCredentials {
            guard finishMutation(generation: generation, target: target) else { return nil }
            status = previousStatus
            return nil
        } catch {
            guard finishMutation(generation: generation, target: target) else { return nil }
            status = previousStatus
            actionErrorMessage = "Couldn’t share \(target.noun). \(error.localizedDescription)"
            errorReporter.report(error, component: "ConversationShare.create")
            return nil
        }
    }

    func revokeShare(target: ConversationShareTarget) async {
        let previousStatus = status
        let generation = beginMutation(target: target)
        do {
            try await apiClient.revokeShare(target: target)
            guard finishMutation(generation: generation, target: target) else { return }
            status = .inactive
        } catch AuthError.authRejected, AuthError.noCredentials {
            guard finishMutation(generation: generation, target: target) else { return }
            status = previousStatus
        } catch {
            guard finishMutation(generation: generation, target: target) else { return }
            status = previousStatus
            actionErrorMessage = "Couldn’t stop sharing. \(error.localizedDescription)"
            errorReporter.report(error, component: "ConversationShare.revoke")
        }
    }

    private func beginMutation(target: ConversationShareTarget) -> Int {
        operationGeneration += 1
        self.target = target
        isUpdating = true
        actionErrorMessage = nil
        return operationGeneration
    }

    private func finishMutation(generation: Int, target: ConversationShareTarget) -> Bool {
        guard generation == operationGeneration, self.target == target else {
            return false
        }
        isUpdating = false
        return true
    }
}

/// Share, re-share or stop sharing one thread or message, from wherever Home
/// offers it. The status is the server's, so this asks before showing a choice.
struct ConversationShareSheet: View {
    let target: ConversationShareTarget

    @Environment(\.dismiss) private var dismiss
    @State private var shareViewModel: ConversationShareViewModel
    @State private var shareURL: URL?

    init(target: ConversationShareTarget, authManager: AuthManager) {
        self.target = target
        _shareViewModel = State(
            initialValue: ConversationShareViewModel(
                apiClient: ChatAPIClient(authManager: authManager)
            )
        )
    }

    private var title: String {
        switch target {
        case .conversation: "Share Conversation"
        case .thread: "Share Thread"
        case .message: "Share Message"
        }
    }

    private var explanation: String {
        let reader = "Anyone with the link who is signed in to Family Assistant can read this \(target.noun)"
        switch target {
        case .message:
            return "\(reader), without the rest of the conversation or how the assistant worked it out."
        case .conversation, .thread:
            return "\(reader), including messages added later."
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(explanation)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section {
                    actions
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task(id: target) {
                await shareViewModel.loadStatus(target: target)
            }
            .sheet(
                item: Binding(
                    get: { shareURL.map(ShareURL.init(url:)) },
                    set: { if $0 == nil { shareURL = nil } }
                )
            ) { item in
                ShareSheet(activityItems: [item.url])
            }
            .alert(
                "Sharing Error",
                isPresented: Binding(
                    get: { shareViewModel.actionErrorMessage != nil },
                    set: { if !$0 { shareViewModel.actionErrorMessage = nil } }
                )
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(verbatim: shareViewModel.actionErrorMessage ?? "")
            }
        }
        .presentationDetents([.medium])
    }

    @ViewBuilder
    private var actions: some View {
        switch shareViewModel.status {
        case .unavailable, .loading:
            ProgressView()
                .frame(maxWidth: .infinity)
        case .inactive:
            shareButton(label: "Create Link")
        case .active:
            shareButton(label: "Replace Link")
            Button("Stop Sharing", role: .destructive) {
                Task { await shareViewModel.revokeShare(target: target) }
            }
            .disabled(shareViewModel.isUpdating)
            .accessibilityIdentifier("stop-sharing")
        case .failed:
            Button("Retry Loading Sharing Status") {
                Task { await shareViewModel.loadStatus(target: target) }
            }
            .accessibilityIdentifier("retry-sharing-status")
        }
    }

    private func shareButton(label: String) -> some View {
        Button(label) {
            Task { shareURL = await shareViewModel.createShare(target: target) }
        }
        .disabled(shareViewModel.isUpdating)
        .accessibilityIdentifier("create-share-link")
    }
}

@MainActor
@Observable
final class SharedConversationViewModel {
    private(set) var messages: [ChatMessage] = []
    private(set) var isSingleMessage = false
    private(set) var loadState: SharedConversationLoadState = .loading
    var attachmentErrorMessage: String?

    @ObservationIgnored private let authManager: AuthManager
    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private let errorReporter: ErrorReporter
    @ObservationIgnored private var loadGeneration = 0

    init(
        authManager: AuthManager,
        apiClient: ChatAPIClient,
        errorReporter: ErrorReporter = .shared
    ) {
        self.authManager = authManager
        self.apiClient = apiClient
        self.errorReporter = errorReporter
    }

    func load(token: String) async {
        loadGeneration += 1
        let generation = loadGeneration
        loadState = .loading
        do {
            let response = try await apiClient.getSharedConversationMessages(token: token)
            guard generation == loadGeneration else { return }
            messages = ChatViewModel.groupToolCallTurns(
                ChatViewModel.renderMessages(from: response.messages)
            )
            isSingleMessage = response.shareScope == "message"
            loadState = .ready
        } catch let ChatAPIError.server(statusCode, _, _) where statusCode == 404 {
            guard generation == loadGeneration else { return }
            messages = []
            loadState = .unavailable
        } catch let ChatAPIError.server(statusCode, _, _) where statusCode == 403 {
            guard generation == loadGeneration else { return }
            messages = []
            loadState = .authenticationRequired
        } catch AuthError.authRejected, AuthError.noCredentials {
            guard generation == loadGeneration else { return }
            messages = []
            loadState = .authenticationRequired
        } catch {
            guard generation == loadGeneration else { return }
            messages = []
            loadState = .failed(error.localizedDescription)
            errorReporter.report(error, component: "SharedConversation.load")
        }
    }
}

extension SharedConversationViewModel: ChatAttachmentLoading {
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
            errorReporter.report(error, component: "SharedConversation.downloadAttachment")
            return nil
        }
    }
}

struct SharedConversationView: View {
    let authManager: AuthManager
    let token: String
    let onClose: () -> Void

    @State private var viewModel: SharedConversationViewModel

    init(authManager: AuthManager, token: String, onClose: @escaping () -> Void) {
        self.authManager = authManager
        self.token = token
        self.onClose = onClose
        _viewModel = State(
            initialValue: SharedConversationViewModel(
                authManager: authManager,
                apiClient: ChatAPIClient(authManager: authManager)
            )
        )
    }

    var body: some View {
        Group {
            switch viewModel.loadState {
            case .loading:
                ProgressView("Loading conversation…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .ready:
                transcript
            case .unavailable:
                unavailableView
            case .authenticationRequired:
                authenticationRequiredView
            case .failed(let message):
                failedView(message: message)
            }
        }
        .navigationTitle(viewModel.isSingleMessage ? "Shared message" : "Shared conversation")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button(action: onClose) {
                    Label("Chats", systemImage: "chevron.left")
                }
            }
        }
        .task(id: token) {
            await viewModel.load(token: token)
        }
        .onChange(of: authManager.authEpoch) {
            Task {
                await viewModel.load(token: token)
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
        .accessibilityIdentifier("shared-conversation-view")
    }

    private var transcript: some View {
        ScrollView {
            LazyVStack(spacing: 14) {
                Label("Read only", systemImage: "lock.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("shared-conversation-read-only")

                if viewModel.messages.isEmpty {
                    ContentUnavailableView(
                        "No messages",
                        systemImage: "text.bubble",
                        description: Text("This shared conversation is empty.")
                    )
                    .padding(.top, 60)
                } else {
                    ForEach(viewModel.messages) { message in
                        MessageBubble(
                            message: message,
                            attachmentLoader: viewModel,
                            userRoleTitle: "Conversation owner"
                        )
                        .accessibilityIdentifier("shared-conversation-message-\(message.id)")
                    }
                }
            }
            .padding()
        }
        .refreshable {
            await viewModel.load(token: token)
        }
    }

    private var unavailableView: some View {
        ContentUnavailableView {
            Label("Shared conversation unavailable", systemImage: "link.badge.plus")
        } description: {
            Text("The link may be invalid, replaced, or no longer shared.")
        } actions: {
            Button("Try again") {
                Task { await viewModel.load(token: token) }
            }
        }
    }

    private var authenticationRequiredView: some View {
        ContentUnavailableView {
            Label("Sign in to view", systemImage: "person.crop.circle.badge.exclamationmark")
        } description: {
            Text("Shared conversations are available only to authorized Family Assistant users.")
        } actions: {
            Button("Sign in again") {
                authManager.login()
            }
            .disabled(authManager.isLoading)
        }
    }

    private func failedView(message: String) -> some View {
        ContentUnavailableView {
            Label("Couldn’t load conversation", systemImage: "wifi.exclamationmark")
        } description: {
            Text(message)
        } actions: {
            Button("Try again") {
                Task { await viewModel.load(token: token) }
            }
        }
    }
}
