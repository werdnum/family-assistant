import XCTest

@testable import FamilyAssistant

final class VoiceTranscriptTests: XCTestCase {
    private func pairs(_ transcript: VoiceTranscript) -> [(VoiceSpeaker, String)] {
        transcript.entries.map { ($0.speaker, $0.text) }
    }

    func testCoalescesConsecutiveSameSpeakerChunks() {
        var transcript = VoiceTranscript()
        transcript.appendUser("turn on ")
        transcript.appendUser("the lights")
        XCTAssertEqual(transcript.entries.count, 1)
        XCTAssertEqual(transcript.entries.first?.text, "turn on the lights")
    }

    func testSpeakerChangeStartsNewEntry() {
        var transcript = VoiceTranscript()
        transcript.appendUser("hi")
        transcript.appendAssistant("hello")
        transcript.appendAssistant(" there")
        transcript.appendUser("bye")
        let result = pairs(transcript)
        XCTAssertEqual(result.count, 3)
        XCTAssertEqual(result[0].0, .user)
        XCTAssertEqual(result[1].1, "hello there")
        XCTAssertEqual(result[2].0, .user)
    }

    func testEmptyChunksAreIgnored() {
        var transcript = VoiceTranscript()
        transcript.appendUser("")
        transcript.appendAssistant("")
        XCTAssertTrue(transcript.isEmpty)
    }
}
