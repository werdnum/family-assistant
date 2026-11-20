import Foundation
import Testing
import UIKit
@testable import FamilyAssistant

private func makeTinyPNGData() -> Data {
    let renderer = UIGraphicsImageRenderer(size: CGSize(width: 4, height: 4))
    return renderer.pngData { ctx in
        UIColor.systemBlue.setFill()
        ctx.fill(CGRect(x: 0, y: 0, width: 4, height: 4))
    }
}

struct BrowserLiveViewTests {
    @Test
    func decodesBrowserFrameSSEEventWithoutConfusingWithAttachment() throws {
        let parser = SSEParser()
        let raw = "event: browser_frame\n"
            + "data: {\"type\":\"browser_frame\",\"seq\":12,\"turn_id\":\"turn-1\",\"frame_id\":\"7\",\"url\":\"https://example.com/cart\",\"captured_at\":\"2026-11-20T11:30:00Z\",\"tool_name\":\"browser_click\",\"backend_kind\":\"remote\",\"width\":1280,\"height\":720,\"image_url\":\"/api/v1/chat/conversations/c1/browser-view/frame?v=7\"}\n\n"

        let events = parser.append(raw).map { parser.decode($0) }
        try #require(events.count == 1)
        let event = events[0]
        #expect(event.type == .browserFrame)
        #expect(event.seq == 12)
        #expect(event.turnID == "turn-1")
        #expect(event.attachments.isEmpty)
        #expect(event.browserFrame?.frameID == "7")
        #expect(event.browserFrame?.url == "https://example.com/cart")
        #expect(event.browserFrame?.displayURL == "example.com/cart")
        #expect(event.browserFrame?.formattedToolName == "Click")
        #expect(event.browserFrame?.backendKind == "remote")
        #expect(event.browserFrame?.imageURL == "/api/v1/chat/conversations/c1/browser-view/frame?v=7")
    }

    @Test
    func decodesBrowserViewOnConversationAndSubconversationResponses() throws {
        let convJSON = """
        {
          "conversation_id": "conv-1",
          "messages": [],
          "count": 0,
          "total_messages": 0,
          "browser_view": {
            "frame_id": "3",
            "url": "https://news.ycombinator.com/",
            "captured_at": "2026-11-20T11:30:00Z",
            "tool_name": "browser_open",
            "backend_kind": "local",
            "width": 1024,
            "height": 768,
            "image_url": "/api/v1/chat/conversations/conv-1/browser-view/frame?v=3"
          }
        }
        """
        let conv = try JSONDecoder.chatDecoder.decode(
            ChatConversationMessagesResponse.self,
            from: Data(convJSON.utf8)
        )
        #expect(conv.browserView?.frameID == "3")
        #expect(conv.browserView?.backendKind == "local")

        let subJSON = """
        {
          "conversation_id": "conv-1",
          "subconversation_id": "sub-1",
          "messages": [],
          "count": 0,
          "total_messages": 0,
          "browser_view": {
            "frame_id": "4",
            "url": "https://example.org/item",
            "captured_at": "2026-11-20T11:31:00Z",
            "tool_name": "click",
            "backend_kind": "remote",
            "image_url": "/api/v1/chat/conversations/conv-1/subconversations/sub-1/browser-view/frame?v=4"
          }
        }
        """
        let sub = try JSONDecoder.chatDecoder.decode(
            ChatSubconversationMessagesResponse.self,
            from: Data(subJSON.utf8)
        )
        #expect(sub.browserView?.frameID == "4")
        #expect(sub.browserView?.backendKind == "remote")
    }

    @Test
    func decodesHasBrowserViewOnProfileAndDisplayNames() throws {
        let profileJSON = """
        {
          "id": "browser_visual_profile",
          "description": "Visual browser",
          "llm_model": "gemini-3.8-flash",
          "available_tools": ["click", "navigate"],
          "enabled_mcp_servers": [],
          "delegation_only": false,
          "has_browser_view": true
        }
        """
        let profile = try JSONDecoder.chatDecoder.decode(
            ChatProfile.self,
            from: Data(profileJSON.utf8)
        )
        #expect(profile.hasBrowserView == true)

        let delegation = ChatPendingDelegation(
            delegationID: "d1",
            targetProfileID: "browser_visual_profile",
            status: "running",
            requestPreview: "Click checkout",
            createdAt: Date(),
            startedAt: Date(),
            completedAt: nil,
            cancelRequested: false,
            childrenTotal: 0,
            childrenFinished: 0,
            subconversationID: "sub-1"
        )
        #expect(delegation.profileDisplayName == "Visual Browser")
    }

    @Test
    @MainActor
    func controllerLoadsDedupesAndResetsFrames() async throws {
        let pngData = makeTinyPNGData()
        var fetchedPaths: [String] = []
        let controller = BrowserLiveViewController { path in
            fetchedPaths.append(path)
            return pngData
        }

        let frame1 = ChatBrowserViewFrame(
            frameID: "1",
            url: "https://example.com/one",
            toolName: "browser_open",
            backendKind: "local",
            imageURL: "/api/v1/chat/conversations/c1/browser-view/frame?v=1"
        )
        controller.apply(frame: frame1)

        // Wait for async image load task to complete.
        for _ in 0..<50 where controller.currentImage == nil {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(controller.currentImage != nil)
        #expect(controller.latestFrame?.frameID == "1")
        #expect(fetchedPaths == [frame1.imageURL])

        // Reapplying the same frame ID does not re-fetch once loaded.
        controller.apply(frame: frame1)
        try await Task.sleep(for: .milliseconds(20))
        #expect(fetchedPaths.count == 1)

        // Reset clears both metadata and image.
        controller.reset()
        #expect(controller.latestFrame == nil)
        #expect(controller.currentImage == nil)
    }
}
