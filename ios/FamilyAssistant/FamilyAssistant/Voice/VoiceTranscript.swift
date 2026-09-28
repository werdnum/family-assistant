import Foundation

/// Who spoke a transcript line.
enum VoiceSpeaker: String, Equatable {
    case user
    case assistant
    case toolCall = "tool_call"
    case tool
}

/// One line of the live conversation transcript.
struct VoiceTranscriptEntry: Identifiable, Equatable {
    let id = UUID()
    let timestamp = Date()
    let speaker: VoiceSpeaker
    var text: String
    var toolCallID: String? = nil
    var toolName: String? = nil
    var toolArguments: JSONValue? = nil
}

/// Accumulates the streamed input/output transcription chunks Gemini emits into
/// readable lines. Consecutive chunks from the same speaker coalesce into one
/// entry; a speaker change starts a new entry. Used both for live captions and
/// for persisting the session as a conversation.
struct VoiceTranscript: Equatable {
    private(set) var entries: [VoiceTranscriptEntry] = []
    private var canCoalesce = true

    var isEmpty: Bool { entries.isEmpty }

    mutating func appendUser(_ text: String) {
        append(.user, text)
    }

    mutating func appendAssistant(_ text: String) {
        append(.assistant, text)
    }

    mutating func breakCoalescing() {
        canCoalesce = false
    }

    private mutating func append(_ speaker: VoiceSpeaker, _ text: String) {
        guard !text.isEmpty else { return }
        if canCoalesce, let index = entries.indices.last, entries[index].speaker == speaker {
            entries[index].text += text
        } else {
            entries.append(VoiceTranscriptEntry(speaker: speaker, text: text))
        }
        canCoalesce = true
    }
}
