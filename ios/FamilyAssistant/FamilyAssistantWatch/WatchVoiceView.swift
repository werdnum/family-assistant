import SwiftUI

struct WatchVoiceView: View {
    @Environment(AuthManager.self) private var auth
    @Environment(WatchAuthentication.self) private var watchAuthentication
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
        VStack(spacing: 12) {
            Image(systemName: "iphone.and.arrow.forward").font(.title)
            Text("Sign in to Family Assistant on your paired iPhone.")
                .font(.footnote)
            Button("Set up with iPhone") { watchAuthentication.connect() }
                .disabled(watchAuthentication.isConnecting)
            if watchAuthentication.isConnecting { ProgressView("Connecting to iPhone…") }
            if let message = watchAuthentication.message {
                Text(message).font(.footnote)
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
