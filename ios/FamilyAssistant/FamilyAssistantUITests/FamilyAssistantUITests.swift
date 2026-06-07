import XCTest

final class FamilyAssistantUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launchEnvironment = ["FAMILY_ASSISTANT_UITEST_INITIAL_PATH": "/notes"]
        app.launch()
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
}
