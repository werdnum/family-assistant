import SwiftUI
import UIKit

/// Full-screen native voice-conversation UI. Builds its view model from the
/// shared ``AuthManager`` and drives a direct Gemini Live session.
struct VoiceView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    /// Optional processing profile to route the session to (nil → default).
    let profileID: String?
    let onClose: (() -> Void)?

    init(profileID: String? = nil, onClose: (() -> Void)? = nil) {
        self.profileID = profileID
        self.onClose = onClose
    }

    @State private var model: VoiceSessionViewModel?
    @State private var sessionRequestID = UUID()

    var body: some View {
        Group {
            if let model {
                VoiceSessionContent(
                    model: model,
                    onStartNewSession: startNewSession,
                    onClose: close
                )
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Voice")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: sessionRequestID) {
            guard model == nil else { return }
            let api = ChatAPIClient(authManager: authManager)
            let viewModel = VoiceSessionViewModel(
                tokenProvider: api,
                toolExecutor: api,
                transcriptStore: api,
                audio: Self.makeAudioIO(),
                profileID: profileID
            )
            model = viewModel
            await viewModel.start()
        }
        .onDisappear {
            model?.end()
        }
    }

    private func startNewSession() {
        model?.end()
        model = nil
        sessionRequestID = UUID()
    }

    private func close() {
        model?.end()
        model = nil
        sessionRequestID = UUID()
        if let onClose {
            onClose()
        } else {
            dismiss()
        }
    }

    private static func makeAudioIO() -> VoiceAudioIO {
        #if DEBUG && targetEnvironment(simulator)
            if UITestConfiguration.isLiveBackendEnabled {
                return SimulatorVoiceAudioIO()
            }
        #endif
        return VoiceAudioEngine()
    }
}

/// Renders one active/loading/failed voice session.
private struct VoiceSessionContent: View {
    @Environment(\.openURL) private var openURL
    @Bindable var model: VoiceSessionViewModel
    let onStartNewSession: () -> Void
    let onClose: () -> Void

    var body: some View {
        VStack(spacing: 28) {
            statusHeader
            VoiceOrb(
                isAssistantSpeaking: model.isAssistantSpeaking,
                isActive: model.phase == .active,
                inputLevel: model.inputLevel,
                hasRecentInput: model.hasRecentInputLevel
            )
            transcript
            Spacer(minLength: 0)
            controls
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var statusHeader: some View {
        Text(statusText)
            .font(.headline)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .accessibilityIdentifier("voice-status")
    }

    private var statusText: String {
        switch model.phase {
        case .idle, .requestingPermission:
            "Preparing…"
        case .connecting:
            "Connecting…"
        case .active:
            model.isAssistantSpeaking ? "Assistant speaking" : "Listening…"
        case .permissionDenied:
            "Microphone access is required for voice mode."
        case .finished:
            "Conversation ended"
        case .failed(let message):
            message
        }
    }

    @ViewBuilder
    private var transcript: some View {
        // The transcript streams text into its newest row on every partial, so it
        // followed on every token — and it animated the follow, the exact pattern
        // that wedges stack placement under the scene-update watchdog. Route it
        // through the shared sticky-bottom container: it follows the streaming
        // tail while the user is at the bottom, never animates, and leaves them
        // put if they scroll up to re-read. The trigger combines the newest
        // entry's id and its text so both a new row and a streamed partial follow.
        // The transcript is already small and bounded by the session, so use an
        // eager stack; LazyVStack's placement path is the recurring watchdog hot
        // spot when the tail mutates during a user scroll.
        StickyBottomScroll(
            followTrigger: model.transcript.entries.last.map { AnyHashable([$0.id.uuidString, $0.text]) },
            canFollow: { UIApplication.shared.applicationState == .active }
        ) {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(model.transcript.entries) { entry in
                    VoiceTranscriptRow(entry: entry)
                        .id(entry.id)
                }
            }
            .padding(.horizontal, 4)
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var controls: some View {
        switch model.phase {
        case .permissionDenied:
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    openURL(url)
                }
            }
            .buttonStyle(.borderedProminent)
            Button("Close", action: onClose)
                .accessibilityIdentifier("voice-close-button")
        case .failed:
            Button("Close", action: onClose)
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("voice-close-button")
        case .finished:
            Button("Start New Session", action: onStartNewSession)
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("voice-start-new-session-button")
        default:
            HStack(spacing: 40) {
                Button {
                    model.isMuted.toggle()
                } label: {
                    Label(
                        model.isMuted ? "Unmute" : "Mute",
                        systemImage: model.isMuted ? "mic.slash.fill" : "mic.fill"
                    )
                    .labelStyle(.iconOnly)
                    .font(.title2)
                    .frame(width: 64, height: 64)
                    .background(model.isMuted ? Color.red.opacity(0.2) : Color.secondary.opacity(0.15))
                    .clipShape(Circle())
                }
                .accessibilityIdentifier("voice-mute-button")
                .accessibilityLabel(model.isMuted ? "Unmute microphone" : "Mute microphone")

                Button {
                    model.end()
                } label: {
                    Label("End", systemImage: "phone.down.fill")
                        .labelStyle(.iconOnly)
                        .font(.title2)
                        .frame(width: 64, height: 64)
                        .background(Color.red)
                        .foregroundStyle(.white)
                        .clipShape(Circle())
                }
                .accessibilityIdentifier("voice-end-button")
                .accessibilityLabel("End conversation")
            }
            .padding(.bottom, 12)
        }
    }
}

/// A pulsing circle that conveys listening vs. speaking state.
private struct VoiceOrb: View {
    let isAssistantSpeaking: Bool
    let isActive: Bool
    let inputLevel: Double
    let hasRecentInput: Bool

    private let meterThresholds = [0.08, 0.18, 0.32, 0.48, 0.64, 0.82, 1.0]

    var body: some View {
        VStack(spacing: 18) {
            ZStack {
                Circle()
                    .fill(isAssistantSpeaking ? Color.accentColor : Color.blue)
                    .opacity(isActive ? 0.85 : 0.4)
                    .frame(width: 160, height: 160)
                    .scaleEffect(isAssistantSpeaking ? 1.08 : 1.0)
                    .animation(
                        isAssistantSpeaking
                            ? .easeInOut(duration: 0.6).repeatForever(autoreverses: true)
                            : .default,
                        value: isAssistantSpeaking
                    )
                Image(systemName: "waveform")
                    .font(.system(size: 48, weight: .semibold))
                    .foregroundStyle(.white)
            }

            HStack(alignment: .bottom, spacing: 5) {
                ForEach(Array(meterThresholds.enumerated()), id: \.offset) { index, threshold in
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(meterBarColor(index: index, threshold: threshold))
                        .frame(width: 8, height: CGFloat(8 + index * 4))
                        .animation(.easeOut(duration: 0.08), value: inputLevel)
                }
            }
            .frame(height: 38)
            .accessibilityLabel("Microphone level")
            .accessibilityIdentifier("voice-mic-level")
        }
    }

    private func meterBarColor(index: Int, threshold: Double) -> Color {
        guard isActive, hasRecentInput, index == 0 || inputLevel >= threshold else {
            return Color.secondary.opacity(0.25)
        }
        return .green
    }
}

private struct VoiceTranscriptRow: View {
    let entry: VoiceTranscriptEntry

    var body: some View {
        HStack {
            if entry.speaker == .assistant {
                bubble
                Spacer(minLength: 32)
            } else {
                Spacer(minLength: 32)
                bubble
            }
        }
    }

    private var bubble: some View {
        Text(entry.text)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(
                entry.speaker == .assistant
                    ? Color.secondary.opacity(0.15)
                    : Color.accentColor.opacity(0.85)
            )
            .foregroundStyle(entry.speaker == .assistant ? Color.primary : Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .accessibilityIdentifier("voice-transcript-\(entry.speaker.rawValue)")
    }
}
