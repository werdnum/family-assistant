import SwiftUI

struct WatchVoiceView: View {
    @Environment(AuthManager.self) private var auth
    @Environment(\.scenePhase) private var scenePhase
    @State private var session: VoiceSessionViewModel?
    @State private var startRequested = false

    private var canStartRequestedSession: Bool {
        startRequested && auth.isAuthenticated && !auth.authRequired
            && !auth.isBootstrapping && scenePhase == .active
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 12) {
                    if auth.isBootstrapping {
                        ProgressView("Signing in…")
                    } else if !auth.isAuthenticated || auth.authRequired {
                        setup
                    } else if let session {
                        voiceControls(session)
                    } else {
                        Button("Start Voice", systemImage: "mic.fill") { begin() }
                            .buttonStyle(.borderedProminent)
                        Text("Voice chats aren't saved to history.")
                            .font(.footnote)
                        Button("Sign Out") { Task { await auth.logout() } }
                            .font(.footnote)
                    }
                }
                .padding(.horizontal)
            }
            .navigationTitle("Voice")
        }
        .onOpenURL { url in
            if WatchVoiceLaunch.opensVoice(url) { startRequested = true }
        }
        .task(id: canStartRequestedSession) {
            if canStartRequestedSession {
                startRequested = false
                begin()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                startRequested = false
                session?.end()
            }
        }
        .onChange(of: auth.authRequired) { _, required in
            if required { session?.end(); session = nil }
        }
    }

    private var setup: some View {
        @Bindable var auth = auth
        return VStack(spacing: 12) {
            Text("Sign in to Family Assistant on your watch.")
                .font(.footnote)
            TextField("Server URL", text: $auth.serverURL)
                .textContentType(.URL)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .disabled(auth.isLoading)
            Button("Sign In") {
                auth.saveServerURL()
                auth.login()
            }
            .disabled(auth.isLoading || auth.serverURL.isEmpty)
            if auth.isLoading { ProgressView() }
            if let error = auth.errorMessage {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
        }
    }

    @ViewBuilder
    private func voiceControls(_ model: VoiceSessionViewModel) -> some View {
        @Bindable var model = model
        switch model.phase {
        case .idle, .requestingPermission, .connecting:
            ProgressView("Connecting…")
        case .active:
            Text(model.isAssistantSpeaking ? "Speaking…" : model.isMuted ? "Muted" : "Listening…")
            ProgressView(value: model.hasRecentInputLevel && !model.isMuted ? model.inputLevel : 0)
                .tint(.green)
                .accessibilityLabel("Microphone activity")
            Button(model.isMuted ? "Unmute" : "Mute",
                   systemImage: model.isMuted ? "mic.slash.fill" : "mic.fill")
            {
                model.isMuted.toggle()
            }
        case .permissionDenied:
            Text("Allow microphone access in Settings to use voice mode.")
                .font(.footnote)
        case let .failed(message):
            Text(message).font(.footnote).foregroundStyle(.red)
        case .finished:
            Text("Session ended")
        }
        if let entry = model.transcript.entries.last {
            Text(entry.text).font(.footnote).lineLimit(5)
        }
        if model.isTerminal {
            Button("Start Again", systemImage: "mic.fill") { begin() }
            Button("Done") { session = nil }
        } else {
            Button("End", systemImage: "phone.down.fill", role: .destructive) { model.end() }
        }
    }

    private func begin() {
        guard session == nil || session?.isTerminal == true else { return }
        let api = ChatAPIClient(authManager: auth)
        let model = VoiceSessionViewModel(tokenProvider: api, toolExecutor: api)
        session = model
        Task { await model.start() }
    }
}
