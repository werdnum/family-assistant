import SwiftUI
import UIKit

/// Full-screen native voice-conversation UI. Builds its view model from the
/// shared ``AuthManager`` and drives a direct Gemini Live session.
struct VoiceView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    /// Optional processing profile to route the session to (nil → default).
    var profileID: String?

    @State private var model: VoiceSessionViewModel?

    var body: some View {
        Group {
            if let model {
                VoiceSessionContent(model: model)
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Voice")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard model == nil else { return }
            let api = ChatAPIClient(authManager: authManager)
            let viewModel = VoiceSessionViewModel(
                tokenProvider: api,
                toolExecutor: api,
                profileID: profileID
            )
            model = viewModel
            await viewModel.start()
        }
        .onChange(of: model?.phase) { _, phase in
            if phase == .finished {
                dismiss()
            }
        }
        .onDisappear {
            model?.end()
        }
    }
}

/// Renders one active/loading/failed voice session.
private struct VoiceSessionContent: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @Bindable var model: VoiceSessionViewModel

    var body: some View {
        VStack(spacing: 28) {
            statusHeader
            VoiceOrb(isAssistantSpeaking: model.isAssistantSpeaking, isActive: model.phase == .active)
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
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(model.transcript.entries) { entry in
                        VoiceTranscriptRow(entry: entry)
                            .id(entry.id)
                    }
                }
                .padding(.horizontal, 4)
            }
            .onChange(of: model.transcript.entries.last?.text) {
                if let last = model.transcript.entries.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
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
            Button("Close") { dismiss() }
                .accessibilityIdentifier("voice-close-button")
        case .failed:
            Button("Close") { dismiss() }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("voice-close-button")
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
                    dismiss()
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

    var body: some View {
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
        .accessibilityHidden(true)
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
    }
}
