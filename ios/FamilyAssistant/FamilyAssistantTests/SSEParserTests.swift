import XCTest

@testable import FamilyAssistant

final class SSEParserTests: XCTestCase {
    func testParsesChunkBoundariesAndMultilineData() {
        let parser = SSEParser()

        XCTAssertTrue(parser.append("event: text\n").isEmpty)
        let events = parser.append("data: first\ndata: second\n\n")

        XCTAssertEqual(events, [
            ServerSentEvent(event: "text", data: "first\nsecond"),
        ])
    }

    func testDecodesTextToolCallToolResultAndEnd() {
        let parser = SSEParser()
        let rawEvents = parser.append(
            """
            event: text
            data: {"content":"Hello"}

            event: tool_call
            data: {"tool_call":{"id":"call_1","type":"function","function":{"name":"search_notes","arguments":"{\\"q\\":\\"school\\"}"}}}

            event: tool_result
            data: {"tool_call_id":"call_1","result":"Found it","attachments":[{"attachment_id":"att-1","name":"result.pdf","content_url":"/api/attachments/att-1","mime_type":"application/pdf"}]}

            event: end
            data: {}

            """
        ) + parser.flush()
        let decoded = rawEvents.map(parser.decode)

        XCTAssertEqual(decoded.map(\.type), [.text, .toolCall, .toolResult, .end])
        XCTAssertEqual(decoded[0].text, "Hello")
        XCTAssertEqual(decoded[1].toolCall?.displayName, "search_notes")
        XCTAssertEqual(decoded[2].toolCallID, "call_1")
        XCTAssertEqual(decoded[2].toolResult, "Found it")
        XCTAssertEqual(decoded[2].attachments.first?.name, "result.pdf")
    }

    func testDecodesConfirmationEvents() {
        let parser = SSEParser()
        let rawEvents = parser.append(
            """
            event: tool_confirmation_request
            data: {"request_id":"req-1","tool_name":"calendar","tool_call_id":"call-1","confirmation_prompt":"Approve?","timeout_seconds":60,"args":{"title":"Dentist"}}

            event: tool_confirmation_result
            data: {"request_id":"req-1","approved":true}

            """
        ) + parser.flush()
        let decoded = rawEvents.map(parser.decode)

        XCTAssertEqual(decoded.first?.confirmation?.requestID, "req-1")
        XCTAssertEqual(decoded.first?.confirmation?.args["title"]?.displayString, "Dentist")
        XCTAssertEqual(decoded.last?.confirmationResult, ChatConfirmationResult(requestID: "req-1", approved: true))
    }

    func testMalformedJSONBecomesErrorEvent() {
        let parser = SSEParser()
        let event = ServerSentEvent(event: "text", data: "{broken")

        let decoded = parser.decode(event)

        XCTAssertEqual(decoded.type, .error)
        XCTAssertTrue(decoded.errorMessage?.contains("Malformed stream event") == true)
    }

    func testMessageEventWithContentStaysMessageForLiveUpdates() {
        let parser = SSEParser()
        let event = ServerSentEvent(event: "message", data: #"{"content":"new message"}"#)

        let decoded = parser.decode(event)

        XCTAssertEqual(decoded.type, .message)
        XCTAssertNil(decoded.text)
    }

    func testCloseConnectedHeartbeatEventsDecodeWithoutPayload() {
        let parser = SSEParser()

        XCTAssertEqual(parser.decode(ServerSentEvent(event: "close", data: "{}")).type, .close)
        XCTAssertEqual(parser.decode(ServerSentEvent(event: "connected", data: "{}")).type, .connected)
        XCTAssertEqual(parser.decode(ServerSentEvent(event: "heartbeat", data: "{}")).type, .heartbeat)
    }
}
