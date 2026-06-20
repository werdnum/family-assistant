import Foundation

/// Decoded response from `POST /api/gemini/ephemeral-token`.
///
/// The backend mints a single-use Gemini ephemeral auth token (its `name`, which
/// begins with `auth_tokens/`) and returns it alongside the model id, the
/// context-injected system instruction, the confirmation-filtered tool
/// declarations (already in Gemini `functionDeclarations` shape), and the live
/// session config. The app connects directly to Gemini with this token.
struct EphemeralToken: Equatable {
    let token: String
    let expiresAt: Date?
    let model: String
    let systemInstruction: String
    /// Tool declarations exactly as the backend formatted them for Gemini. Each
    /// element is typically `{"functionDeclarations": [...]}`; forwarded verbatim
    /// in the setup message.
    let tools: [JSONValue]
    let config: VoiceLiveConfig
}

extension EphemeralToken: Decodable {
    enum CodingKeys: String, CodingKey {
        case token
        case expiresAt = "expires_at"
        case model
        case systemInstruction = "system_instruction"
        case tools
        case config
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        token = try container.decode(String.self, forKey: .token)
        model = try container.decode(String.self, forKey: .model)
        systemInstruction = try container.decode(String.self, forKey: .systemInstruction)
        tools = try container.decodeIfPresent([JSONValue].self, forKey: .tools) ?? []
        config = try container.decode(VoiceLiveConfig.self, forKey: .config)
        let expiresRaw = try container.decodeIfPresent(String.self, forKey: .expiresAt)
        expiresAt = expiresRaw.flatMap(VoiceLiveDateParser.date(from:))
    }
}

/// The subset of the backend's `GeminiLiveConfig` the native client consumes.
struct VoiceLiveConfig: Equatable, Decodable {
    let voiceName: String
    let maxSessionMinutes: Int
    let inputTranscriptionEnabled: Bool
    let outputTranscriptionEnabled: Bool

    init(
        voiceName: String,
        maxSessionMinutes: Int,
        inputTranscriptionEnabled: Bool,
        outputTranscriptionEnabled: Bool
    ) {
        self.voiceName = voiceName
        self.maxSessionMinutes = maxSessionMinutes
        self.inputTranscriptionEnabled = inputTranscriptionEnabled
        self.outputTranscriptionEnabled = outputTranscriptionEnabled
    }

    private enum CodingKeys: String, CodingKey {
        case voice, session, transcription
    }

    private enum VoiceKeys: String, CodingKey { case name }
    private enum SessionKeys: String, CodingKey { case maxDurationMinutes = "max_duration_minutes" }
    private enum TranscriptionKeys: String, CodingKey {
        case inputEnabled = "input_enabled"
        case outputEnabled = "output_enabled"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)

        if let voice = try? container.nestedContainer(keyedBy: VoiceKeys.self, forKey: .voice) {
            voiceName = try voice.decodeIfPresent(String.self, forKey: .name) ?? Self.defaultVoiceName
        } else {
            voiceName = Self.defaultVoiceName
        }

        if let session = try? container.nestedContainer(keyedBy: SessionKeys.self, forKey: .session) {
            maxSessionMinutes = try session.decodeIfPresent(Int.self, forKey: .maxDurationMinutes)
                ?? Self.defaultMaxSessionMinutes
        } else {
            maxSessionMinutes = Self.defaultMaxSessionMinutes
        }

        if let transcription = try? container.nestedContainer(
            keyedBy: TranscriptionKeys.self,
            forKey: .transcription
        ) {
            inputTranscriptionEnabled = try transcription.decodeIfPresent(Bool.self, forKey: .inputEnabled) ?? true
            outputTranscriptionEnabled = try transcription.decodeIfPresent(Bool.self, forKey: .outputEnabled) ?? true
        } else {
            inputTranscriptionEnabled = true
            outputTranscriptionEnabled = true
        }
    }

    static let defaultVoiceName = "Puck"
    static let defaultMaxSessionMinutes = 15
}

/// A function call requested by Gemini during a live turn.
struct GeminiFunctionCall: Equatable {
    /// Correlates the response back to the request. Absent for some legacy frames.
    let id: String?
    let name: String
    /// Call arguments as an object (or `.null` when the model sends no args).
    let args: JSONValue
}

/// A tool result sent back to Gemini after executing a `GeminiFunctionCall`.
struct GeminiFunctionResponse: Equatable {
    let id: String?
    let name: String
    /// The response payload object, e.g. `{"result": ...}` or `{"error": ...}`.
    let response: JSONValue
}

/// A parsed message from the Gemini Live server. A single wire frame may yield
/// several events (e.g. an audio chunk plus an output transcription plus
/// `turnComplete`), so the codec returns an ordered array.
enum GeminiLiveServerEvent: Equatable {
    /// The session is established; safe to start streaming microphone audio.
    case setupComplete
    /// A chunk of assistant speech: 24 kHz, mono, signed 16-bit little-endian PCM.
    case audio(Data)
    /// Incremental transcript of the user's speech.
    case inputTranscription(String)
    /// Incremental transcript of the assistant's speech.
    case outputTranscription(String)
    /// The assistant finished its current spoken turn.
    case turnComplete
    /// The model finished generating (may precede audio fully draining).
    case generationComplete
    /// The user barged in; any buffered assistant audio should be discarded.
    case interrupted
    /// The model wants to invoke one or more tools.
    case toolCall([GeminiFunctionCall])
    /// Previously issued tool calls were cancelled by these ids.
    case toolCallCancellation([String])
    /// The server is about to drop the connection (session limit, maintenance).
    case goAway(timeLeft: String?)
}

/// Shared ISO-8601 parsing for voice payloads, tolerant of fractional seconds.
enum VoiceLiveDateParser {
    private static let withFractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let plain: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func date(from string: String) -> Date? {
        withFractional.date(from: string) ?? plain.date(from: string)
    }
}
