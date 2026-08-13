import Observation
import SwiftUI

enum SharedConversationLoadState: Equatable {
    case loading
    case ready
    case unavailable
    case authenticationRequired
    case failed(String)
}

@MainActor
@Observable
final class SharedConversationViewModel {
    private(set) var messages: [ChatMessage] = []
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
        .navigationTitle("Shared conversation")
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
