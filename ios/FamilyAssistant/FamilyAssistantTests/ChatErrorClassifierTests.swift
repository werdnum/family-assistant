import Foundation
import XCTest

@testable import FamilyAssistant

/// Table-driven taxonomy tests (§6.2 M3): operation × error → expected surface.
/// The classifier is pure, so every case is a direct call with no view-model
/// wiring.
final class ChatErrorClassifierTests: XCTestCase {
    private func server(_ statusCode: Int, retryAfter: TimeInterval? = nil) -> Error {
        ChatAPIError.server(statusCode: statusCode, detail: nil, retryAfter: retryAfter)
    }

    private func classify(
        _ operation: ChatOperation,
        _ error: Error?,
        userInitiated: Bool = false,
        midTurn: Bool = false,
        retryAfter: TimeInterval? = nil
    ) -> ChatErrorSurface {
        ChatErrorClassifier.classify(
            ChatErrorContext(
                operation: operation,
                error: error,
                userInitiated: userInitiated,
                midTurn: midTurn,
                retryAfter: retryAfter
            )
        )
    }

    // MARK: - Advisory reads never modal

    func testAdvisoryTransportFailuresAreSilent() {
        let advisory: [ChatOperation] = [
            .conversationsRefresh,
            .recentConversationsRefresh,
            .messagesMerge,
            .profilesLoad,
            .pendingApprovalsPoll,
        ]
        let errors: [Error] = [
            URLError(.timedOut),
            URLError(.networkConnectionLost),
            URLError(.cannotConnectToHost),
            server(500),
            server(502),
            server(503),
        ]
        for operation in advisory {
            for error in errors {
                XCTAssertEqual(
                    classify(operation, error),
                    .silent,
                    "advisory \(operation.rawValue) on \(error) must be silent, never modal"
                )
            }
        }
    }

    func testAdvisory408IsSilent() {
        XCTAssertEqual(classify(.recentConversationsRefresh, server(408)), .silent)
    }

    func testPendingApprovalsPollFailureIsNeverModal() {
        // Regression for prod cluster G: the poll's failure used to modal.
        XCTAssertEqual(classify(.pendingApprovalsPoll, server(503)), .silent)
        XCTAssertEqual(classify(.pendingApprovalsPoll, URLError(.timedOut)), .silent)
    }

    // MARK: - User-initiated reads

    func testUserInitiatedReadSurfacesInline() {
        XCTAssertEqual(
            classify(.conversationsRefresh, URLError(.timedOut), userInitiated: true),
            .inlineFeedback(reason: .userReadFailed)
        )
        XCTAssertEqual(
            classify(.conversationsRefresh, server(500), userInitiated: true),
            .inlineFeedback(reason: .userReadFailed)
        )
    }

    // MARK: - User actions

    func testUserActionsSurfaceInlineActionFailed() {
        for operation in [ChatOperation.sendTurn, .stopTurn, .confirmTool, .attachmentOp] {
            XCTAssertEqual(
                classify(operation, URLError(.timedOut), userInitiated: true),
                .inlineFeedback(reason: .actionFailed)
            )
        }
    }

    // MARK: - Status semantics

    func test401RoutesToAuthFlow() {
        XCTAssertEqual(classify(.conversationsRefresh, server(401)), .authFlow)
        XCTAssertEqual(classify(.sendTurn, server(401), userInitiated: true), .authFlow)
    }

    func testAuthTerminalErrorRoutesToAuthFlow() {
        XCTAssertEqual(classify(.conversationsRefresh, AuthError.noCredentials), .authFlow)
        XCTAssertEqual(classify(.pendingApprovalsPoll, AuthError.authRejected), .authFlow)
    }

    func test403OnConversationIsAccessChanged() {
        XCTAssertEqual(
            classify(.messagesMerge, server(403)),
            .inlineFeedback(reason: .accessChanged)
        )
        XCTAssertEqual(
            classify(.messagesLoad, server(403), userInitiated: true),
            .inlineFeedback(reason: .accessChanged)
        )
    }

    func test403OnNonConversationOperationIsNotAccessChanged() {
        // A 403 on the account-global list is not a per-conversation access change.
        XCTAssertEqual(classify(.conversationsRefresh, server(403)), .silent)
    }

    func test404OnConversationIsGone() {
        XCTAssertEqual(classify(.messagesMerge, server(404)), .conversationGone)
        XCTAssertEqual(
            classify(.messagesLoad, server(404), userInitiated: true),
            .conversationGone
        )
    }

    func test404OnListIsNotGone() {
        XCTAssertEqual(classify(.conversationsRefresh, server(404)), .silent)
    }

    func test429AdvisoryHonorsRetryAfter() {
        XCTAssertEqual(
            classify(.recentConversationsRefresh, server(429), retryAfter: 12),
            .retryAfter(12)
        )
        XCTAssertEqual(
            classify(.pendingApprovalsPoll, server(429), retryAfter: nil),
            .retryAfter(nil)
        )
    }

    func test429UserActionTellsToTryAgainLater() {
        XCTAssertEqual(
            classify(.sendTurn, server(429), userInitiated: true, retryAfter: 30),
            .inlineFeedback(reason: .rateLimited)
        )
    }

    func test429UserInitiatedAdvisoryReadStaysInline() {
        // A pull-to-refresh (user-initiated advisory read) that is throttled must
        // surface inline rate-limited feedback, NOT `.retryAfter` — which
        // `handleUserReadFailure` maps to the generic Chat Error modal (§4.5).
        XCTAssertEqual(
            classify(.recentConversationsRefresh, server(429), userInitiated: true, retryAfter: 12),
            .inlineFeedback(reason: .rateLimited)
        )
        XCTAssertEqual(
            classify(.messagesLoad, server(429), userInitiated: true, retryAfter: 12),
            .inlineFeedback(reason: .rateLimited)
        )
    }

    func testAuthWallSurfacesActionableInlineFeedback() {
        // An edge authentication wall is persistent and actionable: it must
        // surface as clear inline feedback on every operation, never as silent
        // background degradation or the generic action-failed message.
        XCTAssertEqual(
            classify(.conversationsRefresh, ChatAPIError.authWall),
            .inlineFeedback(reason: .authWall)
        )
        XCTAssertEqual(
            classify(.conversationsRefresh, ChatAPIError.authWall, userInitiated: true),
            .inlineFeedback(reason: .authWall)
        )
        XCTAssertEqual(
            classify(.sendTurn, ChatAPIError.authWall, userInitiated: true),
            .inlineFeedback(reason: .authWall)
        )
    }

    // MARK: - Clean EOF

    func testCleanEOFPostTurnIsSilentForAdvisoryMerge() {
        XCTAssertEqual(classify(.messagesMerge, nil, midTurn: false), .silent)
    }

    func testCleanEOFMidTurnAdvisoryStaysSilent() {
        // The drop/resume machinery handles mid-turn EOF; it is silent to the user.
        XCTAssertEqual(classify(.messagesMerge, nil, midTurn: true), .silent)
    }

    func testCleanEOFOnUserActionIsSilent() {
        XCTAssertEqual(classify(.sendTurn, nil), .silent)
    }
}
