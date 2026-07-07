import UIKit
import XCTest

final class FamilyAssistantUITests: XCTestCase {
    private var app: XCUIApplication!

    // Post-launch / navigation readiness window. A cold CI simulator can take
    // well over the previous 8s to install, boot, authenticate (mock backend),
    // and render the first screen — a passing run was observed at ~35s total and
    // a sibling run timed out the 8s nav-bar wait. `waitForExistence` returns as
    // soon as the element appears, so a generous bound only adds slack on slow CI
    // without slowing the happy path (mirrors the existing 30s sign-out wait).
    private static let readyTimeout: TimeInterval = 30

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        launch(initialPath: "/notes")
    }

    override func tearDown() {
        app = nil
        super.tearDown()
    }

    func testNotesListSearchAndDetailFlow() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["Shopping"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["School Pickup"].waitForExistence(timeout: 4))

        app.searchFields.firstMatch.tap()
        app.searchFields.firstMatch.typeText("school")

        XCTAssertTrue(app.staticTexts["School Pickup"].waitForExistence(timeout: 2))
        XCTAssertFalse(app.staticTexts["Shopping"].exists)

        app.staticTexts["School Pickup"].tap()

        XCTAssertTrue(app.navigationBars["Note"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Pickup is at 3:15 by the north gate."].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Visibility"].exists)
        XCTAssertTrue(app.staticTexts["parents"].exists)
    }

    func testAddNoteSavesThroughBackendAndShowsDetail() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))

        app.buttons["Add Note"].tap()
        XCTAssertTrue(app.navigationBars["Add Note"].waitForExistence(timeout: 3))

        typeText("Dentist", into: app.textFields["Note title"])

        let editor = app.textViews["note-content-editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 4))
        typeText("Appointment is Tuesday at 10.", into: editor)

        app.buttons["Save Note"].tap()

        XCTAssertTrue(app.navigationBars["Note"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Dentist"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Appointment is Tuesday at 10."].exists)
    }

    func testDeleteNoteRemovesItFromList() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["Shopping"].waitForExistence(timeout: 4))
        app.staticTexts["Shopping"].tap()

        XCTAssertTrue(app.navigationBars["Note"].waitForExistence(timeout: 4))
        app.buttons["Delete Note"].tap()

        let deleteSheet = app.sheets["Delete Shopping?"]
        XCTAssertTrue(deleteSheet.waitForExistence(timeout: 3))
        let confirmDelete = deleteSheet.buttons["Delete Note"]
        XCTAssertTrue(confirmDelete.waitForExistence(timeout: 3))
        confirmDelete.tap()

        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: 4))
        XCTAssertFalse(app.staticTexts["Shopping"].waitForExistence(timeout: 1))
        XCTAssertTrue(app.staticTexts["School Pickup"].exists)
    }

    func testNativeChatLoadsSeededConversationAndToolSurface() {
        relaunch(initialPath: "/chat?conversation_id=web_conv_seed")

        openSeededConversationIfNeeded()

        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["default_assistant"].exists || app.buttons["default_assistant, Profile"].exists)
        attachScreenshot(named: "native-chat-history")
    }

    func testReopenConversationAfterBackNavigation() {
        // Start a fresh chat so the seeded conversation is listed in the sidebar
        // but not auto-opened: the bug only manifests when a row is opened by a
        // tap (driving the list selection) rather than via a deep link.
        relaunch(initialPath: "/chat")

        let row = app.descendants(matching: .any).matching(identifier: "conversation-row-web_conv_seed").firstMatch
        showConversationList(row: row)

        // Open the seeded conversation by tapping its row.
        row.tap()
        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 20))

        // Navigate back to the conversation list (compact width).
        let backButton = app.navigationBars.buttons["Chats"]
        XCTAssertTrue(backButton.waitForExistence(timeout: Self.readyTimeout))
        backButton.tap()

        // Re-open the same conversation. Before the fix the row stayed selected
        // after returning, so the selection value never changed and this tap was
        // a no-op: the thread would never load.
        XCTAssertTrue(row.waitForExistence(timeout: Self.readyTimeout))
        row.tap()
        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 20))
        attachScreenshot(named: "native-chat-reopen-after-back")
    }

    /// Ensures the conversation list is visible. In compact width the fresh chat
    /// detail can be pushed on launch, so pop back to the sidebar if needed.
    private func showConversationList(row: XCUIElement) {
        if row.waitForExistence(timeout: 4) {
            return
        }
        let backButton = app.navigationBars.buttons["Chats"]
        if backButton.waitForExistence(timeout: 4) {
            backButton.tap()
        }
        XCTAssertTrue(row.waitForExistence(timeout: Self.readyTimeout))
    }

    func testNativeChatSendsAndStreamsResponse() {
        relaunch(initialPath: "/chat?conversation_id=web_conv_seed")

        openSeededConversationIfNeeded()
        let composer = app.textFields["chat-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: Self.readyTimeout))
        composer.tap()
        composer.typeText("Hello")
        app.buttons["chat-send-button"].tap()

        XCTAssertTrue(app.staticTexts["Native reply to Hello"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["search_notes"].waitForExistence(timeout: 20))
        attachScreenshot(named: "native-chat-streamed-tool-call")
    }

    /// Regression test for the scene-update watchdog hang (0x8BADF00D) that
    /// killed the app when sending a follow-up after a turn that used a tool
    /// returning a large result (build 23,
    /// scratch/FamilyAssistant-2026-06-27-192549.ips). The seeded conversation's
    /// latest turn carries a very large tool result; opening it and sending a
    /// follow-up must keep the app responsive. Before the fix the main thread
    /// wedges re-laying out the tool group and the streamed reply never arrives
    /// within budget, so the wait below times out (the in-test manifestation of
    /// the device watchdog kill).
    func testFollowUpAfterToolTurnStaysResponsive() {
        relaunch(initialPath: "/chat?conversation_id=web_conv_tool_heavy")

        openConversationIfNeeded(id: "web_conv_tool_heavy")

        // The "Show more" control is produced only by web_conv_tool_heavy's very
        // large (bounded) assistant answer, and sits at the bottom of that bubble
        // where the open-time scroll-to-bottom leaves it visible. Its presence
        // proves both that the *seeded* thread opened (a fresh chat has no
        // messages, so a missed row tap fails here) and that the large message
        // rendered without wedging the main thread.
        // web_conv_tool_heavy ends with this small message, so the open-time
        // scroll-to-bottom leaves it visible. Its presence proves the *seeded*
        // thread opened (a fresh chat has no messages, so a missed row tap fails
        // here) and that the thread — including the huge answer just above it —
        // rendered without wedging the main thread.
        XCTAssertTrue(
            app.staticTexts["Tool-heavy summary complete."].waitForExistence(timeout: 30),
            "Tool-heavy thread did not open/render — main thread likely wedged in layout."
        )

        let composer = app.textFields["chat-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: Self.readyTimeout))
        typeText("Anything else?", into: composer)
        app.buttons["chat-send-button"].tap()

        XCTAssertTrue(
            app.staticTexts["Native reply to Anything else?"].waitForExistence(timeout: 20),
            "Follow-up reply never appeared — the app froze re-laying out the tool-heavy thread."
        )
        attachScreenshot(named: "native-chat-tool-heavy-followup")
    }

    func testNativeChatInitialPromptDeepLinkSendsNewConversation() {
        relaunch(initialPath: "/chat?q=Deep%20link")

        XCTAssertTrue(app.staticTexts["Native reply to Deep link"].waitForExistence(timeout: 20))
        attachScreenshot(named: "native-chat-initial-prompt")
    }

    /// Regression test for external URL delivery. The app installs a custom
    /// scene delegate for home-screen quick actions, which replaces SwiftUI's
    /// internal scene delegate — the one that feeds `.onOpenURL`. URLs opened
    /// from outside the app (the `familyassistant://` scheme, and file "Open in
    /// Family Assistant" hand-offs, which use the same delivery path) must
    /// therefore be forwarded by the custom scene delegate; before that
    /// forwarding existed this test timed out because the deep link was
    /// silently dropped.
    func testExternalURLOpenRoutesIntoChat() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))

        XCUIDevice.shared.system.open(URL(string: "familyassistant://chat?q=External%20link")!)
        confirmOpenLinkAlertIfNeeded()

        XCTAssertTrue(
            app.staticTexts["Native reply to External link"].waitForExistence(timeout: 20),
            "Externally opened familyassistant:// URL was not routed into the chat tab."
        )
        attachScreenshot(named: "external-url-open-chat")
    }

    /// Taps "Open" on the system confirmation alert that can accompany an
    /// externally requested custom-scheme open. The alert is SpringBoard's, so
    /// it is not part of `app`'s element tree.
    private func confirmOpenLinkAlertIfNeeded() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let openButton = springboard.buttons["Open"]
        if openButton.waitForExistence(timeout: 5) {
            openButton.tap()
        }
    }

    /// End-to-end coverage for the file "Open in Family Assistant" hand-off:
    /// a file staged into `OpenURLCenter` at cold launch (where the scene
    /// delegate puts real document opens) must pull the app onto the Chat tab
    /// with the file uploaded as a draft attachment.
    func testSharedFileOpensChatWithDraftAttachment() {
        app.terminate()
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launchEnvironment = [
            "FAMILY_ASSISTANT_UITEST_INITIAL_PATH": "/notes",
            "FAMILY_ASSISTANT_UITEST_SHARED_FILE": "trip-itinerary.txt",
        ]
        app.launch()

        // The share must win over the /notes launch route and land on Chat.
        XCTAssertTrue(
            app.staticTexts["trip-itinerary.txt"].waitForExistence(timeout: Self.readyTimeout),
            "Shared file never appeared as a chat draft attachment."
        )
        XCTAssertTrue(app.tabBars.firstMatch.buttons["Chat"].isSelected)
        attachScreenshot(named: "shared-file-draft-attachment")
    }

    /// Pasting a copied image via the composer's paste control must create a
    /// draft attachment (the TextField itself only accepts text pastes).
    func testPastingImageAttachesDraftAttachment() {
        UIPasteboard.general.image = solidTestImage()
        relaunch(initialPath: "/chat")

        let composer = app.textFields["chat-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: Self.readyTimeout))

        let pasteButton = app.descendants(matching: .any)
            .matching(identifier: "chat-paste-button").firstMatch
        XCTAssertTrue(pasteButton.waitForExistence(timeout: 10))
        pasteButton.tap()

        let draftChip = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'draft-attachment-'"))
            .firstMatch
        XCTAssertTrue(
            draftChip.waitForExistence(timeout: 20),
            "Pasted image never appeared as a chat draft attachment."
        )
        attachScreenshot(named: "pasted-image-draft-attachment")
    }

    private func solidTestImage() -> UIImage {
        let size = CGSize(width: 8, height: 8)
        return UIGraphicsImageRenderer(size: size).image { context in
            UIColor.systemTeal.setFill()
            context.fill(CGRect(origin: .zero, size: size))
        }
    }

    func testTabBarSwitchesBetweenFeatureTabs() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))

        let tabBar = app.tabBars.firstMatch
        XCTAssertTrue(tabBar.buttons["Chat"].waitForExistence(timeout: 4))
        XCTAssertTrue(tabBar.buttons["Voice"].exists)
        XCTAssertTrue(tabBar.buttons["Notes"].exists)
        XCTAssertTrue(tabBar.buttons["Documents"].exists)
        XCTAssertTrue(tabBar.buttons["More"].exists)

        tabBar.buttons["More"].tap()
        XCTAssertTrue(app.navigationBars["More"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Events"].exists)
        attachScreenshot(named: "tab-more-list")

        tabBar.buttons["Documents"].tap()
        XCTAssertTrue(app.navigationBars["Documents"].waitForExistence(timeout: 6))

        tabBar.buttons["Notes"].tap()
        XCTAssertTrue(app.staticTexts["Shopping"].waitForExistence(timeout: 4))
    }

    func testNativeVoiceDeepLinkShowsMicrophoneFeedback() {
        relaunch(initialPath: "/voice")
        acceptMicrophonePermissionIfNeeded()

        XCTAssertTrue(app.navigationBars["Voice"].waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.otherElements["voice-mic-level"].waitForExistence(timeout: 10))
        attachScreenshot(named: "native-voice-microphone-feedback")
    }

    func testMoreTabOpensSettingsAndSignsOut() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))

        openMoreSettings()

        let signOut = app.buttons["settings-sign-out"]
        XCTAssertTrue(signOut.waitForExistence(timeout: 4))
        attachScreenshot(named: "settings-screen")

        signOut.tap()
        // Sign-out awaits a WKWebsiteDataStore cleanup that is slow in CI
        // simulators, so allow a generous window for the sign-in screen.
        XCTAssertTrue(
            app.staticTexts["Enter your server URL to get started"].waitForExistence(timeout: 30)
        )
    }

    func testPerTabNavigationStateIsPreservedAcrossTabSwitches() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: Self.readyTimeout))

        openMoreSettings()

        let tabBar = app.tabBars.firstMatch
        tabBar.buttons["Chat"].tap()
        tabBar.buttons["More"].tap()

        // The More tab kept its pushed Settings screen across the tab switch.
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 4))
    }

    /// Opens the More tab and drills into the Settings screen, scrolling the
    /// long destination list if the Settings row is below the fold.
    private func openMoreSettings() {
        let moreTab = app.tabBars.firstMatch.buttons["More"]
        XCTAssertTrue(moreTab.waitForExistence(timeout: Self.readyTimeout))
        moreTab.tap()

        // Wait for the overflow list itself before hunting for a row: on CI
        // simulators the tab transition and list render lag several seconds,
        // and looking for "Settings" too early is what made this flaky.
        XCTAssertTrue(app.navigationBars["More"].waitForExistence(timeout: Self.readyTimeout))

        // Settings sits below the fold in the overflow list. Scroll it into
        // view, retrying because the list can still be settling after it loads.
        let settings = app.staticTexts["Settings"]
        var attempts = 0
        while !settings.waitForExistence(timeout: 2), attempts < 5 {
            app.swipeUp()
            attempts += 1
        }

        XCTAssertTrue(settings.waitForExistence(timeout: 4))
        settings.tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: Self.readyTimeout))
    }

    func testDeepLinkSelectsDocumentsTab() {
        relaunch(initialPath: "/documents/")

        // A launch deep link must finish the cold start, the AuthManager session
        // bootstrap, and the tab routing before the Documents tab appears. That
        // chain runs long on CI simulators (observed >8s after the app idles), so
        // allow a generous window like the other launch-sensitive assertions.
        XCTAssertTrue(app.navigationBars["Documents"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.tabBars.firstMatch.buttons["Documents"].isSelected)
        attachScreenshot(named: "deep-link-documents")
    }

    private func launch(initialPath: String) {
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launchEnvironment = ["FAMILY_ASSISTANT_UITEST_INITIAL_PATH": initialPath]
        app.launch()
    }

    private func relaunch(initialPath: String) {
        app.terminate()
        launch(initialPath: initialPath)
    }

    private func acceptMicrophonePermissionIfNeeded() {
        addUIInterruptionMonitor(withDescription: "Microphone permission") { alert in
            let allowButton = alert.buttons["Allow"]
            if allowButton.exists {
                allowButton.tap()
                return true
            }
            let okButton = alert.buttons["OK"]
            if okButton.exists {
                okButton.tap()
                return true
            }
            return false
        }
        app.tap()
    }

    private func openSeededConversationIfNeeded() {
        if app.staticTexts["Milk and apples."].waitForExistence(timeout: 2) {
            return
        }
        if app.staticTexts["Milk and apples."].exists {
            return
        }
        if app.staticTexts["Milk and apples."].waitForExistence(timeout: 1) {
            return
        }
        let row = app.descendants(matching: .any)["conversation-row-web_conv_seed"]
        if row.waitForExistence(timeout: 6) {
            row.tap()
        }
    }

    /// Opens a seeded conversation by id when the launch deep link did not
    /// auto-open it (compact width can land on the conversation list). `marker`
    /// is a piece of the thread's content used to detect that it is already open.
    private func openConversationIfNeeded(id: String) {
        // The deep link normally opens the thread directly; in compact width the
        // launch can land on the conversation list, so tap the row if it's shown.
        if app.textFields["chat-composer"].waitForExistence(timeout: 4) {
            return
        }
        let row = app.descendants(matching: .any)["conversation-row-\(id)"]
        if row.waitForExistence(timeout: 6) {
            row.tap()
        }
    }

    /// Types into a field after ensuring it holds keyboard focus. On CI the
    /// first tap can land before a previously focused field resigns first
    /// responder, leaving `typeText` with no focused element; re-tapping until
    /// the element reports keyboard focus makes the flow deterministic.
    private func typeText(_ text: String, into element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        element.tap()
        var attempts = 0
        while !(element.value(forKey: "hasKeyboardFocus") as? Bool ?? false), attempts < 5 {
            element.tap()
            attempts += 1
        }
        XCTAssertTrue(
            element.value(forKey: "hasKeyboardFocus") as? Bool ?? false,
            "element did not gain keyboard focus",
            file: file,
            line: line
        )
        element.typeText(text)
    }

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
