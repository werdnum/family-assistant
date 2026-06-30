import Foundation
import XCTest

@testable import FamilyAssistant

final class GeminiLiveCodecTests: XCTestCase {
    // MARK: - Helpers

    private func jsonObject(_ string: String) throws -> [String: Any] {
        let data = Data(string.utf8)
        let object = try JSONSerialization.jsonObject(with: data)
        return try XCTUnwrap(object as? [String: Any])
    }

    private func makeToken(
        model: String = "gemini-3.1-flash-live-preview",
        voiceName: String = "Puck",
        inputTranscription: Bool = true,
        outputTranscription: Bool = true,
        tools: [JSONValue] = []
    ) -> EphemeralToken {
        EphemeralToken(
            token: "auth_tokens/abc",
            expiresAt: nil,
            model: model,
            systemInstruction: "You are helpful.",
            tools: tools,
            config: VoiceLiveConfig(
                voiceName: voiceName,
                maxSessionMinutes: 15,
                inputTranscriptionEnabled: inputTranscription,
                outputTranscriptionEnabled: outputTranscription
            )
        )
    }

    // MARK: - Setup message

    func testSetupMessageStructure() throws {
        let tools: [JSONValue] = [.object([
            "functionDeclarations": .array([.object(["name": .string("get_weather")])])
        ])]
        let message = try GeminiLiveCodec.setupMessage(for: makeToken(tools: tools))
        let root = try jsonObject(message)
        let setup = try XCTUnwrap(root["setup"] as? [String: Any])

        XCTAssertEqual(setup["model"] as? String, "models/gemini-3.1-flash-live-preview")

        let generationConfig = try XCTUnwrap(setup["generationConfig"] as? [String: Any])
        XCTAssertEqual(generationConfig["responseModalities"] as? [String], ["AUDIO"])
        let voiceName = (((generationConfig["speechConfig"] as? [String: Any])?["voiceConfig"]
            as? [String: Any])?["prebuiltVoiceConfig"] as? [String: Any])?["voiceName"] as? String
        XCTAssertEqual(voiceName, "Puck")

        let parts = try XCTUnwrap((setup["systemInstruction"] as? [String: Any])?["parts"] as? [[String: Any]])
        XCTAssertEqual(parts.first?["text"] as? String, "You are helpful.")

        XCTAssertNotNil(setup["tools"])
        XCTAssertNotNil(setup["inputAudioTranscription"])
        XCTAssertNotNil(setup["outputAudioTranscription"])
    }

    func testSetupMessageOmitsTranscriptionWhenDisabled() throws {
        let message = try GeminiLiveCodec.setupMessage(
            for: makeToken(inputTranscription: false, outputTranscription: false)
        )
        let setup = try XCTUnwrap(try jsonObject(message)["setup"] as? [String: Any])
        XCTAssertNil(setup["inputAudioTranscription"])
        XCTAssertNil(setup["outputAudioTranscription"])
    }

    func testSetupMessageOmitsToolsWhenEmpty() throws {
        let message = try GeminiLiveCodec.setupMessage(for: makeToken(tools: []))
        let setup = try XCTUnwrap(try jsonObject(message)["setup"] as? [String: Any])
        XCTAssertNil(setup["tools"])
    }

    func testQualifiedModelNamePreservesExistingPrefix() {
        XCTAssertEqual(GeminiLiveCodec.qualifiedModelName("models/foo"), "models/foo")
        XCTAssertEqual(GeminiLiveCodec.qualifiedModelName("foo"), "models/foo")
    }

    // MARK: - Realtime / tool frames

    func testAudioMessageCarriesBase64AndMIMEType() throws {
        let pcm = Data([0x01, 0x02, 0x03, 0x04])
        let message = try GeminiLiveCodec.audioMessage(base64PCM16: pcm.base64EncodedString())
        let realtime = try XCTUnwrap(try jsonObject(message)["realtimeInput"] as? [String: Any])
        let mediaChunks = try XCTUnwrap(realtime["mediaChunks"] as? [[String: Any]])
        XCTAssertNil(realtime["audio"])
        XCTAssertEqual(mediaChunks.first?["data"] as? String, pcm.base64EncodedString())
        XCTAssertEqual(mediaChunks.first?["mimeType"] as? String, "audio/pcm;rate=16000")
    }

    func testAudioStreamEndMessage() throws {
        let message = try GeminiLiveCodec.audioStreamEndMessage()
        let realtime = try XCTUnwrap(try jsonObject(message)["realtimeInput"] as? [String: Any])
        XCTAssertEqual(realtime["audioStreamEnd"] as? Bool, true)
    }

    func testToolResponseMessageIncludesIDWhenPresent() throws {
        let message = try GeminiLiveCodec.toolResponseMessage([
            GeminiFunctionResponse(
                id: "call-1",
                name: "get_weather",
                response: .object(["result": .string("sunny")])
            )
        ])
        let toolResponse = try XCTUnwrap(try jsonObject(message)["toolResponse"] as? [String: Any])
        let responses = try XCTUnwrap(toolResponse["functionResponses"] as? [[String: Any]])
        XCTAssertEqual(responses.first?["id"] as? String, "call-1")
        XCTAssertEqual(responses.first?["name"] as? String, "get_weather")
        XCTAssertEqual((responses.first?["response"] as? [String: Any])?["result"] as? String, "sunny")
    }

    func testToolResponseMessageOmitsIDWhenNil() throws {
        let message = try GeminiLiveCodec.toolResponseMessage([
            GeminiFunctionResponse(id: nil, name: "noop", response: .object([:]))
        ])
        let toolResponse = try XCTUnwrap(try jsonObject(message)["toolResponse"] as? [String: Any])
        let responses = try XCTUnwrap(toolResponse["functionResponses"] as? [[String: Any]])
        XCTAssertNil(responses.first?["id"])
    }

    // MARK: - Decoding server frames

    func testDecodeSetupComplete() throws {
        let events = try GeminiLiveCodec.decodeServerMessage(Data(#"{"setupComplete":{}}"#.utf8))
        XCTAssertEqual(events, [.setupComplete])
    }

    func testDecodeAudioTranscriptionAndTurnCompleteInOrder() throws {
        let pcm = Data([0x10, 0x20, 0x30, 0x40])
        let frame = """
        {"serverContent":{"modelTurn":{"parts":[{"inlineData":{"mimeType":"audio/pcm;rate=24000","data":"\(pcm.base64EncodedString())"}}]},"outputTranscription":{"text":"hello"},"turnComplete":true}}
        """
        let events = try GeminiLiveCodec.decodeServerMessage(Data(frame.utf8))
        XCTAssertEqual(events, [.audio(pcm), .outputTranscription("hello"), .turnComplete])
    }

    func testDecodeInputTranscriptionAndInterrupted() throws {
        let frame = #"{"serverContent":{"inputTranscription":{"text":"turn on lights"},"interrupted":true}}"#
        let events = try GeminiLiveCodec.decodeServerMessage(Data(frame.utf8))
        XCTAssertEqual(events, [.inputTranscription("turn on lights"), .interrupted])
    }

    func testDecodeToolCall() throws {
        let frame = #"{"toolCall":{"functionCalls":[{"id":"c1","name":"get_weather","args":{"city":"NYC"}}]}}"#
        let events = try GeminiLiveCodec.decodeServerMessage(Data(frame.utf8))
        XCTAssertEqual(events, [.toolCall([
            GeminiFunctionCall(id: "c1", name: "get_weather", args: .object(["city": .string("NYC")]))
        ])])
    }

    func testDecodeToolCallWithoutArgsDefaultsToNull() throws {
        let frame = #"{"toolCall":{"functionCalls":[{"name":"ping"}]}}"#
        let events = try GeminiLiveCodec.decodeServerMessage(Data(frame.utf8))
        XCTAssertEqual(events, [.toolCall([GeminiFunctionCall(id: nil, name: "ping", args: .null)])])
    }

    func testDecodeToolCallCancellationAndGoAway() throws {
        let cancellation = try GeminiLiveCodec.decodeServerMessage(
            Data(#"{"toolCallCancellation":{"ids":["a","b"]}}"#.utf8)
        )
        XCTAssertEqual(cancellation, [.toolCallCancellation(["a", "b"])])

        let goAway = try GeminiLiveCodec.decodeServerMessage(
            Data(#"{"goAway":{"timeLeft":"5s"}}"#.utf8)
        )
        XCTAssertEqual(goAway, [.goAway(timeLeft: "5s")])
    }

    func testDecodeIgnoresUnknownFrame() throws {
        let events = try GeminiLiveCodec.decodeServerMessage(Data(#"{"unknown":{"foo":1}}"#.utf8))
        XCTAssertTrue(events.isEmpty)
    }

    // MARK: - JSONValue round-trip

    func testJSONValueRoundTripPreservesStructure() throws {
        let original: JSONValue = .object([
            "d": .number(1.5),
            "s": .string("x"),
            "b": .bool(true),
            "null": .null,
            "arr": .array([.number(1), .string("two")]),
        ])
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(JSONValue.self, from: data)
        XCTAssertEqual(decoded, original)
    }
}
