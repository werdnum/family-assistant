import Foundation
import XCTest

@testable import FamilyAssistant

final class ToolRenderingTests: XCTestCase {
    func testRenderMessagesCorrelatesToolCallsResultsAndAttachments() throws {
        let json = """
        [
          {
            "internal_id": 10,
            "role": "assistant",
            "content": "I will check.",
            "timestamp": "2026-06-08T12:00:00Z",
            "tool_calls": [
              {
                "id": "call-1",
                "type": "function",
                "function": {
                  "name": "search_notes",
                  "arguments": "{\\"query\\":\\"school\\"}"
                }
              }
            ]
          },
          {
            "internal_id": 11,
            "role": "tool",
            "content": "{\\"status\\":\\"ok\\"}",
            "timestamp": "2026-06-08T12:00:01Z",
            "tool_call_id": "call-1",
            "attachments": [
              {
                "attachment_id": "att-1",
                "name": "notes.md",
                "content_url": "/api/attachments/att-1",
                "mime_type": "text/markdown",
                "size": 42
              }
            ]
          }
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.renderMessages(from: backendMessages)

        XCTAssertEqual(messages.count, 1)
        let toolCall = try XCTUnwrap(messages.first?.toolCalls.first)
        XCTAssertEqual(toolCall.name, "search_notes")
        XCTAssertEqual(toolCall.resultText, #"{"status":"ok"}"#)
        XCTAssertEqual(toolCall.status, .complete)
        XCTAssertEqual(toolCall.attachments.first?.name, "notes.md")
    }

    func testRenderMessagesKeepsGenericUnknownToolFallback() throws {
        let json = """
        [
          {
            "internal_id": 12,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-06-08T12:00:00Z",
            "tool_calls": [
              {
                "id": "call-unknown",
                "type": "function",
                "arguments": {"value": 1}
              }
            ],
            "metadata": {
              "attachments": [
                {
                  "attachment_id": "att-2",
                  "filename": "image.png",
                  "content_url": "/api/attachments/att-2",
                  "mime_type": "image/png"
                }
              ]
            }
          }
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.renderMessages(from: backendMessages)

        XCTAssertEqual(messages.first?.toolCalls.first?.name, "unknown")
        XCTAssertEqual(messages.first?.toolCalls.first?.argumentsText, #"{"value":1}"#)
        XCTAssertEqual(messages.first?.attachments.first?.type, .image)
    }
}
