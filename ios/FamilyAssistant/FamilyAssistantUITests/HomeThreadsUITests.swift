import XCTest

/// Home and threads against the in-process mock backend with threads on
/// (`FAMILY_ASSISTANT_UITEST_THREADS=1`): Home is the landing screen, its
/// `start_thread` call is a card that opens the thread, and the switcher lists
/// Home, the threads by state and the ordinary conversations.
final class HomeThreadsUITests: XCTestCase {
    private var app: XCUIApplication!

    private static let readyTimeout: TimeInterval = 30

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launchEnvironment = [
            "FAMILY_ASSISTANT_UITEST_INITIAL_PATH": "/chat",
            "FAMILY_ASSISTANT_UITEST_THREADS": "1",
        ]
        app.launch()
    }

    override func tearDown() {
        app = nil
        super.tearDown()
    }

    func testLandsOnHomeWithTheThreadCard() {
        XCTAssertTrue(element("home-title").waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["On it. I've started a thread for the quotes."].waitForExistence(timeout: 20))

        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: 20))
        XCTAssertTrue(card.label.contains("Hot water system quotes"))
        XCTAssertTrue(
            card.label.contains("Waiting on you"),
            "The card shows the thread's state once the thread list has loaded."
        )

        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: 10))
        XCTAssertTrue(
            threadsButton.label.contains("1 waiting"),
            "The threads button badges the one thread waiting on the user."
        )
        attachScreenshot(named: "home-landing")
    }

    func testCardOpensTheThreadAndBackReturnsHome() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(element("thread-origin-banner").waitForExistence(timeout: 20))
        XCTAssertTrue(
            app.staticTexts["Gas or electric heat pump? Heat pumps qualify for a rebate."].waitForExistence(timeout: 20)
        )
        XCTAssertFalse(
            app.buttons["profile-picker"].exists,
            "A thread runs on its own profile, so it offers no profile switch."
        )
        attachScreenshot(named: "thread-from-card")

        app.navigationBars.buttons.element(boundBy: 0).tap()

        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(quotesCard.waitForExistence(timeout: 10))
    }

    func testSwitcherGroupsThreadsAndOpensOne() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        XCTAssertTrue(element("thread-switcher-home").waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["WAITING ON YOU"].waitForExistence(timeout: 10)
            || app.staticTexts["Waiting on you"].exists)
        XCTAssertTrue(element("thread-row-web_conv_thread_quotes").exists)
        XCTAssertTrue(element("thread-row-web_conv_thread_flights").exists)
        XCTAssertTrue(
            element("conversation-row-web_conv_seed").waitForExistence(timeout: 10),
            "Ordinary conversations are listed under Earlier."
        )
        XCTAssertFalse(
            element("conversation-row-web_conv_home").exists,
            "Home is its own row, not an Earlier conversation."
        )
        attachScreenshot(named: "thread-switcher")

        element("thread-row-web_conv_thread_flights").tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Checking Qantas first."].waitForExistence(timeout: 20))

        threadsButtonFromThread()
    }

    func testSwitcherOpensAnEarlierConversation() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        let row = element("conversation-row-web_conv_seed")
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()

        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 20))
        XCTAssertFalse(element("thread-origin-banner").exists)
    }

    func testEchoedOutcomeOpensItsThread() {
        // Found by label, like the card: the bubble's identifier is applied
        // over its contents. The title appears once the thread list loads.
        let header = app.buttons["From thread: Gutter cleaning"]
        XCTAssertTrue(header.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["Booked Sam's Gutters for Saturday at 9am."].exists)
        attachScreenshot(named: "home-echo-row")

        header.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Sam's Gutters can come Saturday at 9am. Booked."].waitForExistence(timeout: 20))
    }

    func testMarkDoneInAThreadUpdatesItsCard() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()

        let menu = element("thread-actions-menu")
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        menu.tap()
        let markDone = app.buttons["Mark Done"]
        XCTAssertTrue(markDone.waitForExistence(timeout: 10))
        markDone.tap()

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(
            waitForLabel(of: quotesCard, containing: "Done, You marked this done"),
            "The card shows the thread closed by the user: \(quotesCard.label)"
        )
        let threadsButton = app.buttons["threads-button"]
        XCTAssertFalse(threadsButton.label.contains("waiting"), "Nothing is waiting on the user any more.")
        attachScreenshot(named: "home-card-marked-done")
    }

    func testOpeningAThreadClearsItsUnreadBadge() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForLabel(of: card, containing: "1 unread"))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()
        XCTAssertTrue(
            app.staticTexts["Gas or electric heat pump? Heat pumps qualify for a rebate."].waitForExistence(timeout: 20)
        )

        app.navigationBars.buttons.element(boundBy: 0).tap()

        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(
            waitForLabel(of: quotesCard, notContaining: "unread"),
            "Reading the thread clears its badge: \(quotesCard.label)"
        )
    }

    func testChatsSwitcherListsDoneThreads() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        XCTAssertTrue(app.navigationBars["Chats"].waitForExistence(timeout: 10))
        let row = element("thread-row-web_conv_thread_gutters")
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(row.label.contains("Done"))
        XCTAssertTrue(row.label.contains("Booked Sam's Gutters"), "An assistant-closed thread shows its outcome.")
    }

    func testNewThreadFromTheSwitcherOpensTheThreadWithItsReply() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()
        let newThread = app.buttons["thread-switcher-new-thread"]
        XCTAssertTrue(newThread.waitForExistence(timeout: 10))
        newThread.tap()

        let prompt = element("start-thread-prompt")
        XCTAssertTrue(prompt.waitForExistence(timeout: 10))
        prompt.tap()
        prompt.typeText("Plan the school holidays")
        attachScreenshot(named: "new-thread-sheet")
        let start = app.buttons["start-thread-confirm"]
        XCTAssertTrue(waitForEnabled(start))
        start.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Native reply to Plan the school holidays"].waitForExistence(timeout: 20))
        attachScreenshot(named: "new-thread-opened")

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        threadsButton.tap()
        XCTAssertTrue(
            app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Plan the school holidays"))
                .firstMatch.waitForExistence(timeout: 10),
            "The switcher lists the new thread."
        )
    }

    func testHomeShowsTheWorkingThreadAndOpensIt() {
        let bar = element("home-working-threads")
        XCTAssertTrue(bar.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(bar.label.contains("Melbourne flights"), bar.label)
        attachScreenshot(named: "home-working-bar")
        bar.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
    }

    func testHomeStartsThreadsAndNeverAConversationBesideThem() {
        let newThread = element("home-new-thread")
        XCTAssertTrue(newThread.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertFalse(app.buttons["New Chat"].exists)
        newThread.tap()

        let prompt = element("start-thread-prompt")
        XCTAssertTrue(prompt.waitForExistence(timeout: 10))
        app.buttons["Cancel"].tap()

        app.buttons["threads-button"].tap()
        XCTAssertTrue(element("thread-switcher-new-thread").waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["New Chat"].exists, "The chat list starts threads, not conversations.")
    }

    func testSendAsThreadWorksAfterTheProfileListFailedAtLaunch() {
        app.terminate()
        app.launchEnvironment["FAMILY_ASSISTANT_UITEST_PROFILE_FAILURES"] = "1"
        app.launch()

        let composer = element("chat-composer")
        XCTAssertTrue(composer.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["On it. I've started a thread for the quotes."].waitForExistence(timeout: 20))
        composer.tap()
        composer.typeText("Compare heat pumps")

        let send = app.buttons["chat-send-button"]
        XCTAssertTrue(waitForEnabled(send))
        send.press(forDuration: 1.2)
        let profile = element("start-thread-profile")
        tapMenuItem(app.buttons["Send To…"], until: profile)
        attachScreenshot(named: "send-as-thread-sheet")

        let confirm = app.buttons["start-thread-confirm"]
        XCTAssertTrue(waitForEnabled(confirm), "The sheet fetches the list the launch missed.")
        confirm.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Native reply to Compare heat pumps"].waitForExistence(timeout: 20))
    }

    func testContinueInAThreadShowsWhereItStartedFrom() {
        let answer = app.staticTexts["Yes, bins go out Tuesday night."]
        XCTAssertTrue(answer.waitForExistence(timeout: Self.readyTimeout))
        answer.press(forDuration: 1.2)
        tapMenuItem(app.buttons["Continue in Thread…"], until: element("start-thread-quote"))
        XCTAssertTrue(
            element("start-thread-include-context").exists,
            "The default profile takes Home conversations, so context is offered."
        )
        let prompt = element("start-thread-prompt")
        prompt.tap()
        prompt.typeText("Remind me on Monday")
        attachScreenshot(named: "continue-in-thread-sheet")
        app.buttons["start-thread-confirm"].tap()

        let banner = element("thread-origin-banner")
        XCTAssertTrue(banner.waitForExistence(timeout: 20))
        XCTAssertTrue(
            waitForLabel(of: banner, containing: "Yes, bins go out Tuesday night."),
            "The thread quotes the message it started from: \(banner.label)"
        )
        XCTAssertTrue(app.staticTexts["Native reply to Remind me on Monday"].waitForExistence(timeout: 20))
        attachScreenshot(named: "continued-thread")
    }

    func testMoveLeavesAMarkerInHomeThatOpensTheThread() {
        let question = app.staticTexts["Is bin night Tuesday this week?"]
        XCTAssertTrue(question.waitForExistence(timeout: Self.readyTimeout))
        // The cards' titles come from the thread list, which the destinations need too.
        XCTAssertTrue(waitForLabel(of: quotesCard, containing: "Waiting on you"))
        question.press(forDuration: 1.2)
        let quotes = element("move-destination-web_conv_thread_quotes")
        tapMenuItem(app.buttons["Move to Thread…"], until: quotes)
        let gutters = element("move-destination-web_conv_thread_gutters")
        XCTAssertTrue(gutters.exists)
        XCTAssertFalse(gutters.isEnabled, "A thread whose profile doesn't take Home conversations is not offered.")
        XCTAssertTrue(gutters.label.contains("doesn’t take Home conversations"), gutters.label)
        XCTAssertFalse(element("move-destination-web_conv_thread_flights").isEnabled, "A working thread is not at rest.")
        attachScreenshot(named: "move-to-thread-sheet")
        quotes.tap()

        let marker = app.buttons["Moved to Hot water system quotes"]
        XCTAssertTrue(marker.waitForExistence(timeout: 20))
        XCTAssertFalse(question.exists, "The moved exchange leaves Home.")
        attachScreenshot(named: "home-moved-marker")
        marker.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Is bin night Tuesday this week?"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Yes, bins go out Tuesday night."].exists)
    }

    func testKeyboardLeftOpenInAThreadDoesNotCoverHomeAfterBack() throws {
        try openQuotesThreadWithKeyboardUp()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        assertHomeComposerIsReachable()
    }

    func testKeyboardLeftOpenInAThreadDoesNotCoverHomeAfterSwipeBack() throws {
        try openQuotesThreadWithKeyboardUp()
        let window = app.windows.firstMatch
        let edge = window.coordinate(withNormalizedOffset: CGVector(dx: 0.01, dy: 0.4))
        edge.press(forDuration: 0.05, thenDragTo: window.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.4)))
        assertHomeComposerIsReachable()
    }

    func testLeavingTheAppPutsTheHomeKeyboardAwayAndKeepsTheDraft() throws {
        XCTAssertTrue(element("home-title").waitForExistence(timeout: Self.readyTimeout))
        let composer = element("chat-composer")
        XCTAssertTrue(composer.waitForExistence(timeout: 20))
        composer.tap()
        guard app.keyboards.firstMatch.waitForExistence(timeout: 5) else {
            throw XCTSkip("The simulator is using a hardware keyboard, so no software keyboard appears.")
        }
        composer.typeText("Half-written")
        leaveAndReturn()

        XCTAssertTrue(waitForNonExistence(of: app.keyboards.firstMatch))
        XCTAssertEqual(composer.value as? String, "Half-written")
        XCTAssertTrue(composer.isHittable)
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertLessThanOrEqual(composer.frame.maxY, app.keyboards.firstMatch.frame.minY + 1)
    }

    func testLeavingTheAppPutsAThreadsKeyboardAway() throws {
        try openQuotesThreadWithKeyboardUp()
        leaveAndReturn()

        XCTAssertTrue(waitForNonExistence(of: app.keyboards.firstMatch))
        XCTAssertTrue(element("chat-composer").isHittable)
    }

    private func leaveAndReturn() {
        XCUIDevice.shared.press(.home)
        XCTAssertTrue(app.wait(for: .runningBackground, timeout: 10))
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
    }

    func testScrollingHomeDismissesTheKeyboard() throws {
        XCTAssertTrue(element("home-title").waitForExistence(timeout: Self.readyTimeout))
        let composer = element("chat-composer")
        XCTAssertTrue(composer.waitForExistence(timeout: 20))
        composer.tap()
        guard app.keyboards.firstMatch.waitForExistence(timeout: 5) else {
            throw XCTSkip("The simulator is using a hardware keyboard, so no software keyboard appears.")
        }
        // Drag the messages down through the keyboard, as a reader would.
        let window = app.windows.firstMatch
        window.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3)).press(
            forDuration: 0.05,
            thenDragTo: window.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.98))
        )
        XCTAssertTrue(waitForNonExistence(of: app.keyboards.firstMatch))
    }

    /// Opens the quotes thread from its card and brings its composer's
    /// keyboard up.
    private func openQuotesThreadWithKeyboardUp() throws {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()
        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        let composer = element("chat-composer")
        XCTAssertTrue(composer.waitForExistence(timeout: 20))
        composer.tap()
        guard app.keyboards.firstMatch.waitForExistence(timeout: 5) else {
            throw XCTSkip("The simulator is using a hardware keyboard, so no software keyboard appears.")
        }
    }

    /// Home's composer is on screen and not under the keyboard, whether or not
    /// the keyboard is still up.
    private func assertHomeComposerIsReachable() {
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(waitForNonExistence(of: element("thread-title")))
        let composer = element("chat-composer")
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        // Let the pop and any keyboard transition settle before measuring.
        _ = waitForNonExistence(of: app.keyboards.firstMatch, timeout: 3)
        attachScreenshot(named: "home-after-thread-with-keyboard")
        let keyboard = app.keyboards.firstMatch
        if keyboard.exists {
            XCTAssertLessThanOrEqual(
                composer.frame.maxY,
                keyboard.frame.minY + 1,
                "Home's composer is under the keyboard."
            )
        }
        XCTAssertTrue(composer.isHittable)
    }

    private func waitForNonExistence(of element: XCUIElement, timeout: TimeInterval = 10) -> Bool {
        let expectation = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    /// Tap a context-menu item once it can take the tap. A tap while the menu
    /// is still animating in is dropped, so tap again while the item is
    /// still showing and what it opens has not appeared.
    private func tapMenuItem(_ item: XCUIElement, until opened: XCUIElement) {
        XCTAssertTrue(item.waitForExistence(timeout: 10))
        for _ in 0 ..< 3 {
            let hittable = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isHittable == true"), object: item)
            _ = XCTWaiter.wait(for: [hittable], timeout: 5)
            if item.exists, item.isHittable {
                item.tap()
            }
            if opened.waitForExistence(timeout: 5) {
                return
            }
        }
        XCTFail("\(item) did not open \(opened)")
    }

    /// Back from a thread opened via the switcher lands on Home.
    private func threadsButtonFromThread() {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
    }

    /// The card for the thread Home's `start_thread` call started. Found by its
    /// label: the message bubble's identifier is applied over its contents.
    private var quotesCard: XCUIElement {
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Hot water system quotes")).firstMatch
    }

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    private func waitForLabel(
        of element: XCUIElement,
        containing text: String? = nil,
        notContaining excluded: String? = nil
    ) -> Bool {
        var conditions: [NSPredicate] = []
        if let text {
            conditions.append(NSPredicate(format: "label CONTAINS %@", text))
        }
        if let excluded {
            conditions.append(NSPredicate(format: "NOT (label CONTAINS %@)", excluded))
        }
        let predicate = NSCompoundPredicate(andPredicateWithSubpredicates: conditions)
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: 20) == .completed
    }

    private func waitForEnabled(_ element: XCUIElement) -> Bool {
        let predicate = NSPredicate(format: "isEnabled == true")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: 20) == .completed
    }

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

