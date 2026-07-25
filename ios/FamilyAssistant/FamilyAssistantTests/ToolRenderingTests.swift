import Foundation
import SwiftUI
import UIKit
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

    // MARK: - Inline response images

    /// The reported bug: a reply delivered through a chat interface persists a
    /// bare `attachment_reference`, and before the read path resolved it the
    /// client had nothing to go on and drew a paperclip. With the mime type and
    /// URL present the image is hoisted into the reply itself, rather than
    /// living on a strip below it.
    func testAssistantMessageImageAttachmentIsHoistedInline() throws {
        let messages = try renderMessages("""
        [
          {
            "internal_id": 20,
            "role": "assistant",
            "content": "Here is the chart.",
            "timestamp": "2026-07-01T12:00:00Z",
            "attachments": [
              {
                "type": "attachment_reference",
                "attachment_id": "att-image",
                "mime_type": "image/png",
                "content_url": "/api/attachments/att-image",
                "description": "chart.png"
              }
            ]
          }
        ]
        """)

        let images = try XCTUnwrap(messages.first?.inlineImageAttachments)
        XCTAssertEqual(images.map(\.attachmentID), ["att-image"])
        XCTAssertEqual(images.first?.name, "chart.png")
        XCTAssertEqual(images.first?.contentURL, "/api/attachments/att-image")
    }

    /// A tool-produced image comes back on the tool row, which `renderMessages`
    /// folds into the tool call. The tool group collapses once its calls
    /// complete, so leaving the image there hides it on reload; it has to be
    /// hoisted out of the group like the message's own attachments.
    func testToolResultImageIsHoistedOutOfTheCollapsedToolGroup() throws {
        let messages = try renderMessages("""
        [
          {
            "internal_id": 30,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-07-01T12:00:00Z",
            "tool_calls": [
              {"id": "call-chart", "type": "function",
               "function": {"name": "generate_chart", "arguments": "{}"}}
            ]
          },
          {
            "internal_id": 31,
            "role": "tool",
            "content": "{\\"status\\":\\"ok\\"}",
            "timestamp": "2026-07-01T12:00:01Z",
            "tool_call_id": "call-chart",
            "attachments": [
              {
                "type": "tool_result",
                "attachment_id": "att-chart",
                "mime_type": "image/png",
                "content_url": "/api/attachments/att-chart",
                "description": "chart.png"
              }
            ]
          }
        ]
        """)

        let message = try XCTUnwrap(messages.first)
        XCTAssertEqual(message.toolCalls.first?.attachments.map(\.attachmentID), ["att-chart"])
        XCTAssertEqual(message.inlineImageAttachments.map(\.attachmentID), ["att-chart"])
    }

    /// A persisted tool-result row records the mime type but not always a URL.
    /// The id is enough to address it, so the image still renders.
    func testImageWithoutContentURLResolvesItFromTheAttachmentID() throws {
        let messages = try renderMessages("""
        [
          {
            "internal_id": 40,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-07-01T12:00:00Z",
            "attachments": [
              {"type": "tool_result", "attachment_id": "att with space", "mime_type": "image/jpeg"}
            ]
          }
        ]
        """)

        XCTAssertEqual(
            messages.first?.inlineImageAttachments.map(\.contentURL),
            ["/api/attachments/att%20with%20space"]
        )
    }

    /// The live turn appends the response attachment to the assistant bubble,
    /// while the persisted rows carry the same attachment on the tool row. A
    /// message holding both must show the image once.
    func testImageReportedByBothTheMessageAndItsToolCallRendersOnce() {
        let image = ChatAttachment(
            id: "att-dup",
            attachmentID: "att-dup",
            type: .image,
            name: "photo.png",
            contentURL: "/api/attachments/att-dup",
            mimeType: "image/png",
            size: nil,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        let message = ChatMessage(
            id: "msg-dup",
            role: .assistant,
            text: "Done.",
            createdAt: Date(),
            toolCalls: [
                ChatToolCall(
                    id: "call-1",
                    name: "attach_to_response",
                    argumentsText: "{}",
                    resultText: "ok",
                    attachments: [image],
                    status: .complete
                ),
            ],
            attachments: [image],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil
        )

        XCTAssertEqual(message.inlineImageAttachments.map(\.attachmentID), ["att-dup"])
    }

    /// Only images are hoisted. A document keeps its strip, where its name and
    /// download affordance live, and an attachment with no mime type is not
    /// guessed at — nothing here can prove it is an image.
    func testNonImageAndUnprovableAttachmentsAreNotHoisted() throws {
        let messages = try renderMessages("""
        [
          {
            "internal_id": 50,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-07-01T12:00:00Z",
            "attachments": [
              {"type": "tool_result", "attachment_id": "att-doc", "mime_type": "application/pdf"},
              {"type": "attachment_reference", "attachment_id": "att-unknown"}
            ]
          }
        ]
        """)

        let message = try XCTUnwrap(messages.first)
        XCTAssertTrue(message.inlineImageAttachments.isEmpty)
        XCTAssertEqual(message.attachments.map(\.type), [.document, .file])
    }

    /// A bubble is a whole agentic turn, so a turn full of image-producing tool
    /// calls could otherwise stack an unbounded number of full-width images into
    /// one message. The overflow is capped rather than dropped: what is not
    /// hoisted stays on its own attachment strip.
    func testInlineImagesAreCappedWithTheRemainderLeftOnTheirStrip() {
        let toolCalls = (0..<10).map { index in
            ChatToolCall(
                id: "call-\(index)",
                name: "take_snapshot",
                argumentsText: "{}",
                resultText: "ok",
                attachments: [
                    ChatAttachment(
                        id: "att-\(index)",
                        attachmentID: "att-\(index)",
                        type: .image,
                        name: "snapshot-\(index).png",
                        contentURL: "/api/attachments/att-\(index)",
                        mimeType: "image/png",
                        size: nil,
                        localFileURL: nil,
                        uploadState: .uploaded,
                        errorMessage: nil
                    ),
                ],
                status: .complete
            )
        }
        let message = ChatMessage(
            id: "msg-many",
            role: .assistant,
            text: "Here they are.",
            createdAt: Date(),
            toolCalls: toolCalls,
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil
        )

        let inline = message.inlineImageAttachments
        XCTAssertEqual(inline.count, ChatMessage.maxInlineImages)
        XCTAssertEqual(inline.map(\.attachmentID), (0..<ChatMessage.maxInlineImages).map { "att-\($0)" })

        // The uncapped remainder is still reachable: it is not in the hoisted
        // set, so the tool call's own strip keeps rendering it.
        let hoisted = Set(inline.map(\.dedupeKey))
        let remaining = message.toolCalls.flatMap(\.attachments).filter { !hoisted.contains($0.dedupeKey) }
        XCTAssertEqual(remaining.map(\.attachmentID), (ChatMessage.maxInlineImages..<10).map { "att-\($0)" })
    }

    /// The user's own uploads already render on the user bubble's strip.
    func testUserUploadsAreNotHoisted() throws {
        let messages = try renderMessages("""
        [
          {
            "internal_id": 60,
            "role": "user",
            "content": "Look at this",
            "timestamp": "2026-07-01T12:00:00Z",
            "attachments": [
              {
                "type": "user",
                "attachment_id": "att-upload",
                "mime_type": "image/png",
                "content_url": "/api/attachments/att-upload"
              }
            ]
          }
        ]
        """)

        XCTAssertTrue(try XCTUnwrap(messages.first).inlineImageAttachments.isEmpty)
    }

    /// The collector decides *what* to show; this checks the bubble actually
    /// draws it. Hosts the production message list against a stubbed backend
    /// serving a real PNG and samples the rendered pixels for the image's own
    /// colour — a paperclip, a placeholder, or an image left inside the
    /// collapsed tool group all fail this.
    @MainActor
    func testAssistantBubbleDrawsTheResponseImageItself() throws {
        let imageColor = UIColor(red: 1, green: 0, blue: 1, alpha: 1)
        seedAuth()
        defer { clearAuth() }
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        defer {
            ChatMockBackendURLProtocol.reset()
            URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        }
        let pngData = try XCTUnwrap(Self.solidImage(color: imageColor).pngData())
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path.hasPrefix("/api/attachments/") == true else {
                return .json("{}", statusCode: 404)
            }
            return ChatMockResponse(statusCode: 200, data: pngData, headers: ["Content-Type": "image/png"])
        }

        // The image arrives on the tool row, as it does after a reload — the
        // shape that is otherwise buried in the collapsed tool group.
        let messages = try renderMessages("""
        [
          {
            "internal_id": 70,
            "role": "assistant",
            "content": "Here is the chart.",
            "timestamp": "2026-07-01T12:00:00Z",
            "tool_calls": [
              {"id": "call-chart", "type": "function",
               "function": {"name": "generate_chart", "arguments": "{}"}}
            ]
          },
          {
            "internal_id": 71,
            "role": "tool",
            "content": "{\\"status\\":\\"ok\\"}",
            "timestamp": "2026-07-01T12:00:01Z",
            "tool_call_id": "call-chart",
            "attachments": [
              {
                "type": "tool_result",
                "attachment_id": "att-chart",
                "mime_type": "image/png",
                "content_url": "/api/attachments/att-chart",
                "description": "chart.png"
              }
            ]
          }
        ]
        """)

        let viewModel = ChatViewModel(
            authManager: Self.mockAuthManager(),
            errorReporter: ErrorReporter(spoolDirectory: nil)
        )
        let host = UIHostingController(
            rootView: ChatMessageListLayoutProbe(messages: messages, viewModel: viewModel)
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 393, height: 852))
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.view.frame = window.bounds
        defer {
            window.isHidden = true
            window.rootViewController = nil
        }

        // The fetch is asynchronous, so pump until the image lands rather than
        // sleeping for a fixed interval.
        var snapshot = Self.snapshot(of: window)
        let deadline = Date(timeIntervalSinceNow: 10)
        while Date() < deadline, !Self.contains(color: imageColor, in: snapshot) {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))
            host.view.setNeedsLayout()
            host.view.layoutIfNeeded()
            snapshot = Self.snapshot(of: window)
        }

        let attachment = XCTAttachment(image: snapshot)
        attachment.name = "assistant-bubble-with-response-image"
        attachment.lifetime = .keepAlways
        add(attachment)

        XCTAssertTrue(
            Self.contains(color: imageColor, in: snapshot),
            "The response image was not drawn in the assistant bubble."
        )
    }

    /// Cached image bytes are private to the session that fetched them: a
    /// different deployment must never hit an entry cached from another (the
    /// same attachment id can exist on both, e.g. a restored database), and
    /// logging out must drop them so the next person on this device cannot be
    /// handed them without a re-authorized download.
    func testAttachmentImageCacheIsScopedToItsServerAndClearable() throws {
        let attachment = ChatAttachment(
            id: "att-cache",
            attachmentID: "att-cache",
            type: .image,
            name: "photo.png",
            contentURL: "/api/attachments/att-cache",
            mimeType: "image/png",
            size: nil,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        let image = Self.solidImage(color: .red)
        defer { AttachmentImageCache.clear() }

        AttachmentImageCache.store(image, for: attachment, server: "https://one.example.test")

        XCTAssertNotNil(AttachmentImageCache.image(for: attachment, server: "https://one.example.test"))
        XCTAssertNil(AttachmentImageCache.image(for: attachment, server: "https://two.example.test"))

        AttachmentImageCache.clear()
        XCTAssertNil(AttachmentImageCache.image(for: attachment, server: "https://one.example.test"))
    }

    /// With no server configured there is nothing to scope an entry to, so
    /// nothing is cached.
    func testAttachmentImageCacheIgnoresEntriesWithoutAServer() {
        let attachment = ChatAttachment(
            id: "att-nowhere",
            attachmentID: "att-nowhere",
            type: .image,
            name: "photo.png",
            contentURL: "/api/attachments/att-nowhere",
            mimeType: "image/png",
            size: nil,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        defer { AttachmentImageCache.clear() }

        AttachmentImageCache.store(Self.solidImage(color: .green), for: attachment, server: "")

        XCTAssertNil(AttachmentImageCache.image(for: attachment, server: ""))
    }

    private func seedAuth() {
        KeychainHelper.save(key: "fa_api_token", string: "render-test-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
    }

    private func clearAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    @MainActor
    private static func mockAuthManager() -> AuthManager {
        let authManager = AuthManager()
        // Matches ChatMockBackendURLProtocol's host filter.
        authManager.serverURL = "https://assistant.example.test"
        return authManager
    }

    private static func solidImage(color: UIColor, size: CGSize = CGSize(width: 64, height: 64)) -> UIImage {
        UIGraphicsImageRenderer(size: size).image { context in
            color.setFill()
            context.fill(CGRect(origin: .zero, size: size))
        }
    }

    /// Renders through the layer tree rather than `drawHierarchy`, which returns
    /// a blank image for a window that was never attached to a scene.
    @MainActor
    private static func snapshot(of window: UIWindow) -> UIImage {
        UIGraphicsImageRenderer(bounds: window.bounds).image { context in
            window.layer.render(in: context.cgContext)
        }
    }

    /// Whether any pixel matches `color`, allowing for rendering/colour-space
    /// rounding.
    private static func contains(color: UIColor, in image: UIImage, tolerance: Int = 12) -> Bool {
        guard let cgImage = image.cgImage else {
            return false
        }
        let width = cgImage.width
        let height = cgImage.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        guard let context = CGContext(
            data: &pixels,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            return false
        }
        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        var red: CGFloat = 0
        var green: CGFloat = 0
        var blue: CGFloat = 0
        var alpha: CGFloat = 0
        color.getRed(&red, green: &green, blue: &blue, alpha: &alpha)
        let target = (
            red: Int(red * 255),
            green: Int(green * 255),
            blue: Int(blue * 255)
        )
        for index in stride(from: 0, to: pixels.count, by: 4) {
            if abs(Int(pixels[index]) - target.red) <= tolerance,
               abs(Int(pixels[index + 1]) - target.green) <= tolerance,
               abs(Int(pixels[index + 2]) - target.blue) <= tolerance
            {
                return true
            }
        }
        return false
    }

    private func renderMessages(_ json: String) throws -> [ChatMessage] {
        let backendMessages = try JSONDecoder.chatDecoder.decode(
            [ChatBackendMessage].self,
            from: Data(json.utf8)
        )
        return ChatViewModel.renderMessages(from: backendMessages)
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
