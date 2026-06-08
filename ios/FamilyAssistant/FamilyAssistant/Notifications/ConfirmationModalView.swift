import Observation
import SwiftUI

/// A confirmation request surfaced by tapping the body of a confirmation push notification.
///
/// Carries the identifiers and the notification's own title/body so the modal can render
/// immediately, before (or even if) the full details are fetched from the server.
struct PendingConfirmationModal: Identifiable, Equatable {
    let requestID: String
    let conversationID: String?
    let title: String
    let body: String

    var id: String { requestID }
}

@MainActor
@Observable
final class ConfirmationModalModel {
    enum LoadState: Equatable {
        case loading
        case loaded(ChatConfirmationDetail)
        case failed(String)
    }

    let request: PendingConfirmationModal
    private(set) var loadState: LoadState = .loading
    var isSubmitting = false
    var errorMessage: String?
    private(set) var didResolve = false

    @ObservationIgnored private let apiClient: ChatAPIClient

    init(request: PendingConfirmationModal, authManager: AuthManager) {
        self.request = request
        apiClient = ChatAPIClient(authManager: authManager)
    }

    var detail: ChatConfirmationDetail? {
        if case .loaded(let detail) = loadState {
            return detail
        }
        return nil
    }

    /// Whether to offer the approve/reject buttons.
    ///
    /// A request that resolved or expired elsewhere (another device, the action buttons, or a
    /// timeout) is shown read-only. When the detail fetch fails we still offer the buttons and fall
    /// back to the notification payload, letting the server arbitrate the decision rather than
    /// stranding the user.
    var canDecide: Bool {
        guard !didResolve else {
            return false
        }
        switch loadState {
        case .loaded(let detail):
            return detail.status.isPending
        case .failed:
            return true
        case .loading:
            return false
        }
    }

    var promptText: String {
        detail?.confirmationPrompt ?? request.body
    }

    var statusMessage: String? {
        guard let detail else {
            return nil
        }
        switch detail.status {
        case .pending:
            return didResolve ? "This request has been handled." : nil
        case .approved:
            return "This request was already approved."
        case .rejected:
            return "This request was already rejected."
        case .expired:
            return "This request has expired."
        case .unknown:
            return "This request is no longer available."
        }
    }

    func load() async {
        loadState = .loading
        do {
            let detail = try await apiClient.fetchConfirmation(requestID: request.requestID)
            loadState = .loaded(detail)
        } catch {
            loadState = .failed(error.localizedDescription)
        }
    }

    /// Submit the user's decision. Returns ``true`` when the modal should dismiss.
    func submit(approved: Bool) async -> Bool {
        guard !isSubmitting else {
            return false
        }
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }

        do {
            try await apiClient.confirmTool(
                requestID: request.requestID,
                conversationID: request.conversationID,
                approved: approved
            )
            didResolve = true
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }
}

struct ConfirmationModalView: View {
    @State private var model: ConfirmationModalModel
    let onFinished: () -> Void

    init(request: PendingConfirmationModal, authManager: AuthManager, onFinished: @escaping () -> Void) {
        _model = State(initialValue: ConfirmationModalModel(request: request, authManager: authManager))
        self.onFinished = onFinished
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    header
                    Text(model.promptText)
                        .font(.body)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityIdentifier("confirmation-modal-prompt")

                    if let detail = model.detail, !detail.args.isEmpty {
                        argumentsView(detail.args)
                    }

                    if let statusMessage = model.statusMessage {
                        Label(statusMessage, systemImage: "info.circle")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .accessibilityIdentifier("confirmation-modal-status")
                    }

                    if let errorMessage = model.errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline)
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("confirmation-modal-error")
                    }

                    if case .failed = model.loadState {
                        Label("Could not load the latest details. You can still respond below.", systemImage: "wifi.exclamationmark")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding()
            }
            .overlay {
                if case .loading = model.loadState {
                    ProgressView()
                }
            }
            .safeAreaInset(edge: .bottom) {
                actionButtons
            }
            .navigationTitle("Confirmation")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { onFinished() }
                        .accessibilityIdentifier("confirmation-modal-close")
                }
            }
        }
        .task {
            await model.load()
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "hand.raised.fill")
                .font(.title2)
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.detail?.toolName ?? model.request.title)
                    .font(.headline)
                if let detail = model.detail, detail.status.isPending, detail.timeRemainingSeconds > 0 {
                    Text("Expires in \(Int(detail.timeRemainingSeconds))s")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func argumentsView(_ args: [String: JSONValue]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Details")
                .font(.caption.bold())
                .foregroundStyle(.secondary)
            Text(JSONValue.object(args).jsonString)
                .font(.caption.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 8))
                .textSelection(.enabled)
        }
        .accessibilityIdentifier("confirmation-modal-args")
    }

    @ViewBuilder
    private var actionButtons: some View {
        if model.canDecide {
            HStack(spacing: 12) {
                Button(role: .destructive) {
                    Task {
                        if await model.submit(approved: false) {
                            onFinished()
                        }
                    }
                } label: {
                    Label("Reject", systemImage: "xmark")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("confirmation-modal-reject")

                Button {
                    Task {
                        if await model.submit(approved: true) {
                            onFinished()
                        }
                    }
                } label: {
                    Label("Approve", systemImage: "checkmark")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("confirmation-modal-approve")
            }
            .disabled(model.isSubmitting)
            .padding()
            .background(.bar)
        }
    }
}
