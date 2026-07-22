import XCTest

/// Scene-wiring coverage for the M2 iOS state-sync work (design §6.2 "UI tests").
///
/// These cases drive the app through standard XCUITest backgrounding
/// (`XCUIDevice` home-press / `activate`) against the scriptable UI-test backend
/// (`UITestBackendURLProtocol`) and assert WIRING/UI outcomes only:
///
///   * no "Chat Error" modal appears across a background/foreground cycle,
///   * the live-updates indicator is not stuck in a disconnected state,
///   * a conversation-list row added server-side while backgrounded surfaces on
///     resume without a manual refresh,
///   * a mid-session stream drop recovers without an alert.
///
/// Per the design's known limitation, simulator home-press does NOT faithfully
/// reproduce socket death, so these tests never assert on socket teardown — only
/// on the reconciliation wiring and its user-visible surface. Timeouts are
/// generous and there are no tight timing assertions, matching the retried CI
/// lane's launch/render slack.
final class ChatSceneWiringUITests: XCTestCase {
    private var app: XCUIApplication!

    private static let readyTimeout: TimeInterval = 30
    private static let recoveryTimeout: TimeInterval = 40

    /// Accessibility identifiers the `LiveUpdatesIndicator` exposes only in a
    /// disconnected/attention presentation state. Their absence means the
    /// indicator is either `.live`/`.suspended` (no indicator) or transiently
    /// `.syncing`, never stuck disconnected.
    private static let disconnectedIndicatorIDs = [
        "chat-live-updates-degraded",
        "chat-live-updates-offline",
        "chat-live-updates-auth-required",
    ]

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    override func tearDown() {
        app = nil
        super.tearDown()
    }

    /// (a) Background then foreground: no "Chat Error" alert, and the connection
    /// indicator is not stuck disconnected. Both live streams hang open, so the
    /// steady state is `.live` and the foreground resync reconnects them.
    func testBackgroundForegroundKeepsConnectionHealthy() {
        launch(
            initialPath: "/chat?conversation_id=web_conv_seed",
            environment: ["FAMILY_ASSISTANT_UITEST_STREAM_BEHAVIOR": "hang"]
        )
        openSeededConversation()

        backgroundThenForeground()

        // The seeded thread is still shown (the app resumed into its content) and
        // no error modal was raised by the resume.
        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: Self.recoveryTimeout))
        assertNoChatErrorAlert()
        assertIndicatorNotStuckDisconnected()
    }

    /// (b) A conversation-list row added server-side while backgrounded appears
    /// after foregrounding without a manual refresh. The backend reveals the row
    /// on list fetches once the activity stream has reconnected (the foreground
    /// resync's connect), so its presence proves the resume reconciled the list.
    func testConversationAddedWhileBackgroundedAppearsAfterForeground() {
        launch(
            initialPath: "/chat",
            environment: [
                "FAMILY_ASSISTANT_UITEST_STREAM_BEHAVIOR": "hang",
                "FAMILY_ASSISTANT_UITEST_BACKGROUNDED_CONVERSATION": "web_conv_added",
            ]
        )

        let addedRow = row(id: "web_conv_added")
        showConversationList()
        // The row is hidden until the app has been backgrounded and foregrounded.
        XCTAssertFalse(addedRow.exists, "Added conversation surfaced before any resume.")

        backgroundThenForeground()
        showConversationList()

        XCTAssertTrue(
            addedRow.waitForExistence(timeout: Self.recoveryTimeout),
            "Conversation added while backgrounded did not appear after foregrounding."
        )
        assertNoChatErrorAlert()
    }

    /// (c) A mid-session stream drop (`dropAfterN`) recovers without an alert. The
    /// first follow connect drops after a couple of heartbeats; the coordinator's
    /// reconnect loop re-establishes a hanging stream and returns to `.live`.
    func testMidSessionStreamDropRecoversWithoutAlert() {
        launch(
            initialPath: "/chat?conversation_id=web_conv_seed",
            environment: [
                "FAMILY_ASSISTANT_UITEST_STREAM_BEHAVIOR": "dropAfterN",
                "FAMILY_ASSISTANT_UITEST_STREAM_DROP_AFTER": "2",
            ]
        )
        openSeededConversation()

        // A drop must never raise the shared error modal, and the reconnect loop
        // must clear any transient disconnected indicator within a backoff window.
        assertNoChatErrorAlert()
        assertIndicatorRecovers()
    }

    /// A `503burst` on the activity stream recovers to a healthy indicator once
    /// the burst clears, again without an alert.
    func testActivityStreamBurst503RecoversWithoutAlert() {
        launch(
            initialPath: "/chat?conversation_id=web_conv_seed",
            environment: [
                "FAMILY_ASSISTANT_UITEST_STREAM_BEHAVIOR": "503burst",
                "FAMILY_ASSISTANT_UITEST_503_BURST_COUNT": "1",
            ]
        )
        openSeededConversation()

        assertNoChatErrorAlert()
        assertIndicatorRecovers()
    }

    // MARK: - Backgrounding

    private func backgroundThenForeground() {
        XCUIDevice.shared.press(.home)
        // Give the scene the moment it needs to observe `.background` before the
        // reactivation; the resync latches on the observed background.
        _ = app.wait(for: .runningBackground, timeout: Self.readyTimeout)
        app.activate()
        XCTAssertTrue(
            app.wait(for: .runningForeground, timeout: Self.readyTimeout),
            "App did not return to the foreground."
        )
    }

    // MARK: - Assertions

    private func assertNoChatErrorAlert() {
        // A short existence check: the alert, if it were raised, presents
        // synchronously with the failure. `waitForExistence` returning false is
        // the pass.
        XCTAssertFalse(
            app.alerts["Chat Error"].waitForExistence(timeout: 3),
            "A \"Chat Error\" modal appeared — a transport event surfaced as a user alert."
        )
    }

    private func assertIndicatorNotStuckDisconnected() {
        for identifier in Self.disconnectedIndicatorIDs {
            let indicator = app.descendants(matching: .any)[identifier]
            XCTAssertFalse(
                indicator.exists,
                "Live-updates indicator stuck disconnected (\(identifier))."
            )
        }
    }

    /// Polls until no disconnected indicator remains, tolerating a transient
    /// disconnected/syncing window during reconnect. The backoff is a couple of
    /// seconds, so the recovery bound is generous.
    private func assertIndicatorRecovers() {
        let deadline = Date().addingTimeInterval(Self.recoveryTimeout)
        while Date() < deadline {
            let anyDisconnected = Self.disconnectedIndicatorIDs.contains { identifier in
                app.descendants(matching: .any)[identifier].exists
            }
            if !anyDisconnected {
                return
            }
            _ = app.staticTexts["__never__"].waitForExistence(timeout: 1)
        }
        assertIndicatorNotStuckDisconnected()
    }

    // MARK: - Navigation helpers

    private func openSeededConversation() {
        if app.staticTexts["Milk and apples."].waitForExistence(timeout: Self.readyTimeout) {
            return
        }
        let seededRow = row(id: "web_conv_seed")
        if seededRow.waitForExistence(timeout: 6) {
            seededRow.tap()
        }
        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: Self.readyTimeout))
    }

    /// Ensures the conversation list is visible (compact width can push a fresh
    /// detail on launch).
    private func showConversationList() {
        let anyRow = row(id: "web_conv_seed")
        if anyRow.waitForExistence(timeout: 4) {
            return
        }
        let backButton = app.navigationBars.buttons["Chats"]
        if backButton.waitForExistence(timeout: 4) {
            backButton.tap()
        }
    }

    private func row(id: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: "conversation-row-\(id)").firstMatch
    }

    private func launch(initialPath: String, environment: [String: String]) {
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        var launchEnvironment = environment
        launchEnvironment["FAMILY_ASSISTANT_UITEST_INITIAL_PATH"] = initialPath
        app.launchEnvironment = launchEnvironment
        app.launch()
    }
}
