import Foundation

/// Encodes outgoing Gemini Live `BidiGenerateContent` frames and decodes incoming
/// server messages. Pure and stateless so it can be unit-tested without a socket.
///
/// The wire shapes mirror what the `@google/genai` SDK (used by the web client)
/// sends and receives: JSON text frames with camelCase keys.
enum GeminiLiveCodec {
    /// 16 kHz mono signed-16 PCM is the only input format the Live API accepts.
    static let inputAudioMIMEType = "audio/pcm;rate=16000"

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return encoder
    }()

    // MARK: - Outgoing frames

    /// The first frame on a new connection: model, audio response config, system
    /// instruction, tools, and transcription toggles.
    static func setupMessage(for token: EphemeralToken) throws -> String {
        var setup: [String: JSONValue] = [
            "model": .string(qualifiedModelName(token.model)),
            "generationConfig": .object([
                "responseModalities": .array([.string("AUDIO")]),
                "speechConfig": .object([
                    "voiceConfig": .object([
                        "prebuiltVoiceConfig": .object([
                            "voiceName": .string(token.config.voiceName)
                        ])
                    ])
                ]),
            ]),
            "systemInstruction": .object([
                "parts": .array([.object(["text": .string(token.systemInstruction)])])
            ]),
        ]
        if !token.tools.isEmpty {
            setup["tools"] = .array(token.tools)
        }
        if token.config.inputTranscriptionEnabled {
            setup["inputAudioTranscription"] = .object([:])
        }
        if token.config.outputTranscriptionEnabled {
            setup["outputAudioTranscription"] = .object([:])
        }
        return try encode(.object(["setup": .object(setup)]))
    }

    /// A chunk of microphone audio (base64 of 16 kHz mono PCM16).
    static func audioMessage(base64PCM16: String) throws -> String {
        try encode(.object([
            "realtimeInput": .object([
                "audio": .object([
                    "data": .string(base64PCM16),
                    "mimeType": .string(inputAudioMIMEType),
                ])
            ])
        ]))
    }

    /// Signals that the client has stopped sending audio for now.
    static func audioStreamEndMessage() throws -> String {
        try encode(.object([
            "realtimeInput": .object(["audioStreamEnd": .bool(true)])
        ]))
    }

    /// Returns tool results to the model so it can continue the turn.
    static func toolResponseMessage(_ responses: [GeminiFunctionResponse]) throws -> String {
        let encoded = responses.map { response -> JSONValue in
            var object: [String: JSONValue] = [
                "name": .string(response.name),
                "response": response.response,
            ]
            if let id = response.id {
                object["id"] = .string(id)
            }
            return .object(object)
        }
        return try encode(.object([
            "toolResponse": .object(["functionResponses": .array(encoded)])
        ]))
    }

    // MARK: - Incoming frames

    /// Decodes one server frame into zero or more ordered events. A frame can carry
    /// an audio chunk, transcriptions, and turn-control signals at once.
    static func decodeServerMessage(_ data: Data) throws -> [GeminiLiveServerEvent] {
        let root = try JSONDecoder().decode(JSONValue.self, from: data)
        var events: [GeminiLiveServerEvent] = []

        if root["setupComplete"] != nil {
            events.append(.setupComplete)
        }

        if let serverContent = root["serverContent"] {
            decodeServerContent(serverContent, into: &events)
        }

        if let functionCalls = root["toolCall"]?["functionCalls"]?.arrayValue {
            let calls = functionCalls.compactMap(decodeFunctionCall)
            if !calls.isEmpty {
                events.append(.toolCall(calls))
            }
        }

        if let ids = root["toolCallCancellation"]?["ids"]?.arrayValue {
            events.append(.toolCallCancellation(ids.compactMap(\.stringValue)))
        }

        if let goAway = root["goAway"] {
            events.append(.goAway(timeLeft: goAway["timeLeft"]?.stringValue))
        }

        return events
    }

    private static func decodeServerContent(
        _ serverContent: JSONValue,
        into events: inout [GeminiLiveServerEvent]
    ) {
        if let parts = serverContent["modelTurn"]?["parts"]?.arrayValue {
            for part in parts {
                guard let inlineData = part["inlineData"],
                      let base64 = inlineData["data"]?.stringValue,
                      let mimeType = inlineData["mimeType"]?.stringValue,
                      mimeType.hasPrefix("audio/"),
                      let pcm = Data(base64Encoded: base64)
                else {
                    continue
                }
                events.append(.audio(pcm))
            }
        }

        if let text = serverContent["inputTranscription"]?["text"]?.stringValue, !text.isEmpty {
            events.append(.inputTranscription(text))
        }
        if let text = serverContent["outputTranscription"]?["text"]?.stringValue, !text.isEmpty {
            events.append(.outputTranscription(text))
        }

        if serverContent["interrupted"]?.boolValue == true {
            events.append(.interrupted)
        }
        if serverContent["generationComplete"]?.boolValue == true {
            events.append(.generationComplete)
        }
        if serverContent["turnComplete"]?.boolValue == true {
            events.append(.turnComplete)
        }
    }

    private static func decodeFunctionCall(_ value: JSONValue) -> GeminiFunctionCall? {
        guard let name = value["name"]?.stringValue else {
            return nil
        }
        return GeminiFunctionCall(
            id: value["id"]?.stringValue,
            name: name,
            args: value["args"] ?? .null
        )
    }

    // MARK: - Helpers

    /// Gemini Live expects a fully-qualified `models/<id>` model name.
    static func qualifiedModelName(_ model: String) -> String {
        model.hasPrefix("models/") ? model : "models/\(model)"
    }

    private static func encode(_ value: JSONValue) throws -> String {
        let data = try encoder.encode(value)
        return String(decoding: data, as: UTF8.self)
    }
}
