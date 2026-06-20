import Foundation

/// Who spoke a transcript line.
enum VoiceSpeaker: String, Equatable {
    case user
    case assistant
}

/// One line of the live conversation transcript.
struct VoiceTranscriptEntry: Identifiable, Equatable {
    let id = UUID()
    let speaker: VoiceSpeaker
    var text: String
}

/// Accumulates the streamed input/output transcription chunks Gemini emits into
/// readable lines. Consecutive chunks from the same speaker coalesce into one
/// entry; a speaker change starts a new entry. Used both for live captions and
/// for persisting the session as a conversation.
struct VoiceTranscript: Equatable {
    private(set) var entries: [VoiceTranscriptEntry] = []

    var isEmpty: Bool { entries.isEmpty }

    mutating func appendUser(_ text: String) {
        append(.user, text)
    }

    mutating func appendAssistant(_ text: String) {
        append(.assistant, text)
    }

    private mutating func append(_ speaker: VoiceSpeaker, _ text: String) {
        guard !text.isEmpty else { return }
        if let index = entries.indices.last, entries[index].speaker == speaker {
            entries[index].text += text
        } else {
            entries.append(VoiceTranscriptEntry(speaker: speaker, text: text))
        }
    }
}
