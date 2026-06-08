import XCTest

final class FamilyAssistantUITests: XCTestCase {
    private var app: XCUIApplication!

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
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.staticTexts["Shopping"].exists)
        XCTAssertTrue(app.staticTexts["School Pickup"].exists)

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
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: 8))

        app.buttons["Add Note"].tap()
        XCTAssertTrue(app.navigationBars["Add Note"].waitForExistence(timeout: 3))

        app.textFields["Note title"].tap()
        app.textFields["Note title"].typeText("Dentist")

        let editor = app.textViews.firstMatch
        XCTAssertTrue(editor.waitForExistence(timeout: 2))
        editor.tap()
        editor.typeText("Appointment is Tuesday at 10.")

        app.buttons["Save Note"].tap()

        XCTAssertTrue(app.navigationBars["Note"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Dentist"].waitForExistence(timeout: 4))
        XCTAssertTrue(app.staticTexts["Appointment is Tuesday at 10."].exists)
    }

    func testDeleteNoteRemovesItFromList() {
        XCTAssertTrue(app.navigationBars["Notes"].waitForExistence(timeout: 8))
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

        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["default_assistant"].exists || app.buttons["default_assistant, Profile"].exists)
        attachScreenshot(named: "native-chat-history")
    }

    func testNativeChatSendsAndStreamsResponse() {
        relaunch(initialPath: "/chat?conversation_id=web_conv_seed")

        openSeededConversationIfNeeded()
        let composer = app.textFields["chat-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 8))
        composer.tap()
        composer.typeText("Hello")
        app.buttons["chat-send-button"].tap()

        XCTAssertTrue(app.staticTexts["Native reply to Hello"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.staticTexts["search_notes"].waitForExistence(timeout: 8))
        attachScreenshot(named: "native-chat-streamed-tool-call")
    }

    func testNativeChatInitialPromptDeepLinkSendsNewConversation() {
        relaunch(initialPath: "/chat?q=Deep%20link")

        XCTAssertTrue(app.staticTexts["Native reply to Deep link"].waitForExistence(timeout: 8))
        attachScreenshot(named: "native-chat-initial-prompt")
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

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
