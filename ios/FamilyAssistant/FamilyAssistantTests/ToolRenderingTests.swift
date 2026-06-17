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

    /// An agentic turn that loops over many tool calls is persisted as one
    /// backend assistant message per step. Those steps must collapse into a
    /// single tool group so the thread shows one collapsible box for the turn
    /// rather than one box per tool call (matching the web client).
    func testRenderMessagesGroupsConsecutiveToolCallTurnsIntoOneBubble() throws {
        let toolSteps = (0..<30).map { index in
            """
            {
              "internal_id": \(100 + index),
              "role": "assistant",
              "content": "",
              "timestamp": "2026-06-08T12:\(String(format: "%02d", index)):00Z",
              "tool_calls": [
                {"id": "call-\(index)", "type": "function",
                 "function": {"name": "search_notes", "arguments": "{}"}}
              ]
            },
            {
              "internal_id": \(200 + index),
              "role": "tool",
              "content": "{\\"ok\\":true}",
              "timestamp": "2026-06-08T12:\(String(format: "%02d", index)):00Z",
              "tool_call_id": "call-\(index)"
            }
            """
        }
        let json = """
        [
          {"internal_id": 1, "role": "user", "content": "Find everything",
           "timestamp": "2026-06-08T11:59:00Z"},
          \(toolSteps.joined(separator: ",\n")),
          {"internal_id": 999, "role": "assistant", "content": "Here is the summary.",
           "timestamp": "2026-06-08T12:31:00Z"}
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.groupToolCallTurns(ChatViewModel.renderMessages(from: backendMessages))

        // user bubble, one grouped tool bubble, and the final text answer.
        XCTAssertEqual(messages.map(\.role), [.user, .assistant, .assistant])
        let toolBubble = try XCTUnwrap(messages.first { $0.role == .assistant && !$0.toolCalls.isEmpty })
        XCTAssertEqual(toolBubble.toolCalls.count, 30)
        XCTAssertEqual(toolBubble.toolCalls.map(\.id), (0..<30).map { "call-\($0)" })
        let answer = try XCTUnwrap(messages.last)
        XCTAssertEqual(answer.text, "Here is the summary.")
        XCTAssertTrue(answer.toolCalls.isEmpty)
    }

    /// Tool calls from different turns (separated by a user message) must stay in
    /// separate bubbles rather than collapsing across the turn boundary.
    func testRenderMessagesDoesNotGroupToolCallsAcrossTurns() throws {
        let json = """
        [
          {"internal_id": 1, "role": "user", "content": "First", "timestamp": "2026-06-08T12:00:00Z"},
          {"internal_id": 2, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:00:01Z",
           "tool_calls": [{"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
          {"internal_id": 3, "role": "user", "content": "Second", "timestamp": "2026-06-08T12:00:02Z"},
          {"internal_id": 4, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:00:03Z",
           "tool_calls": [{"id": "b", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.groupToolCallTurns(ChatViewModel.renderMessages(from: backendMessages))

        XCTAssertEqual(messages.map(\.role), [.user, .assistant, .user, .assistant])
        XCTAssertEqual(messages.filter { !$0.toolCalls.isEmpty }.map { $0.toolCalls.map(\.id) }, [["a"], ["b"]])
    }

    /// The grouped bubble is dated by its newest folded-in step.
    func testGroupedToolCallBubbleTimestampReflectsLatestStep() throws {
        let json = """
        [
          {"internal_id": 1, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:00:00Z",
           "tool_calls": [{"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
          {"internal_id": 2, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:05:00Z",
           "tool_calls": [{"id": "b", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.groupToolCallTurns(ChatViewModel.renderMessages(from: backendMessages))

        XCTAssertEqual(messages.count, 1)
        let expected = ISO8601DateFormatter().date(from: "2026-06-08T12:05:00Z")
        XCTAssertEqual(messages.first?.createdAt, expected)
    }

    /// `renderMessages` itself stays one-to-one with persisted backend messages
    /// (grouping is applied separately, for display), so the delta-merge cursor
    /// and per-message identity are preserved.
    func testRenderMessagesKeepsBackendMessagesOneToOne() throws {
        let json = """
        [
          {"internal_id": 1, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:00:00Z",
           "tool_calls": [{"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
          {"internal_id": 2, "role": "assistant", "content": "", "timestamp": "2026-06-08T12:05:00Z",
           "tool_calls": [{"id": "b", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
        ]
        """
        let backendMessages = try JSONDecoder.chatDecoder.decode([ChatBackendMessage].self, from: Data(json.utf8))

        let messages = ChatViewModel.renderMessages(from: backendMessages)

        XCTAssertEqual(messages.map(\.id), ["msg_1", "msg_2"])
        XCTAssertEqual(messages.map { $0.toolCalls.map(\.id) }, [["a"], ["b"]])
    }
}
