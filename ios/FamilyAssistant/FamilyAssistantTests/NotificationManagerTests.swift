import Foundation
import UserNotifications
import XCTest

@testable import FamilyAssistant

@MainActor
final class NotificationManagerTests: XCTestCase {
    override func setUp() {
        super.setUp()
        resetStoredAuth()
        resetStoredNotificationState()
        URLProtocol.registerClass(NotificationBackendURLProtocol.self)
        NotificationBackendURLProtocol.reset()
    }

    override func tearDown() {
        NotificationBackendURLProtocol.reset()
        URLProtocol.unregisterClass(NotificationBackendURLProtocol.self)
        resetStoredAuth()
        resetStoredNotificationState()
        super.tearDown()
    }

    func testInitialStateReflectsStoredNotificationPreference() {
        UserDefaults.standard.set(true, forKey: "fa_notifications_enabled")

        let manager = NotificationManager()

        XCTAssertTrue(manager.notificationsEnabled)
        XCTAssertEqual(manager.registrationState, .registering)
    }

    func testStatusLabelReflectsAuthorizationAndRegistrationState() {
        let manager = NotificationManager()

        manager.authorizationStatus = .notDetermined
        manager.notificationsEnabled = false
        XCTAssertEqual(manager.statusLabel, "Off")

        manager.notificationsEnabled = true
        XCTAssertEqual(manager.statusLabel, "Permission needed")

        manager.authorizationStatus = .authorized
        manager.registrationState = .registering
        XCTAssertEqual(manager.statusLabel, "Registering")

        manager.registrationState = .registered
        XCTAssertEqual(manager.statusLabel, "On")

        manager.registrationState = .failed
        XCTAssertEqual(manager.statusLabel, "Needs attention")

        manager.authorizationStatus = .denied
        XCTAssertEqual(manager.statusLabel, "Denied")
    }

    func testHandleDeepLinkNormalizesQueryPathCustomSchemeAndAbsoluteURL() throws {
        let manager = NotificationManager()

        XCTAssertTrue(manager.handleDeepLink(try XCTUnwrap(URL(string: "familyassistant://open?path=/notes/add"))))
        XCTAssertEqual(manager.pendingNavigationPath, "/notes/add")

        manager.clearPendingNavigationPath()
        XCTAssertNil(manager.pendingNavigationPath)

        XCTAssertTrue(manager.handleDeepLink(try XCTUnwrap(URL(string: "familyassistant://chat?conversation_id=abc123"))))
        XCTAssertEqual(manager.pendingNavigationPath, "/chat?conversation_id=abc123")

        manager.clearPendingNavigationPath()
        XCTAssertTrue(manager.handleDeepLink(try XCTUnwrap(URL(string: "https://assistant.example.test/notes/edit/School"))))
        XCTAssertEqual(manager.pendingNavigationPath, "/notes/edit/School")
    }

    func testHandleDeepLinkRejectsProtocolRelativePathQueryWithoutHostFallback() throws {
        let manager = NotificationManager()

        XCTAssertFalse(manager.handleDeepLink(try XCTUnwrap(URL(string: "familyassistant:?path=//evil.example/path"))))
        XCTAssertNil(manager.pendingNavigationPath)
    }

    func testAPNsRegistrationStoresHexTokenWithoutSyncWhenNotificationsAreDisabled() {
        let manager = NotificationManager()
        manager.notificationsEnabled = false
        manager.registrationState = .disabled

        manager.handleAPNsRegistration(deviceToken: Data([0x00, 0x0f, 0xff]))

        XCTAssertEqual(UserDefaults.standard.string(forKey: "fa_apns_device_token"), "000fff")
        XCTAssertEqual(manager.registrationState, .disabled)
        XCTAssertNil(manager.errorMessage)
    }

    func testAPNsRegistrationFailureMarksManagerFailed() {
        let manager = NotificationManager()

        manager.handleAPNsRegistrationFailure(URLError(.notConnectedToInternet))

        XCTAssertEqual(manager.registrationState, .failed)
        XCTAssertEqual(manager.errorMessage, URLError(.notConnectedToInternet).localizedDescription)
    }

    func testEnabledAPNsRegistrationSyncsTokenWithServer() async throws {
        seedStoredAuth()
        let authManager = makeAuthManager()
        let manager = NotificationManager()
        manager.bind(authManager: authManager)
        manager.notificationsEnabled = true
        let requestCompleted = expectation(description: "push token registered")
        NotificationBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/ios/push-tokens")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer notification-api-token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["token"] as? String, "deadbeef")
            XCTAssertEqual(payload["environment"] as? String, "sandbox")
            XCTAssertNotNil(payload["bundle_id"])
            XCTAssertNotNil(payload["installation_id"])
            XCTAssertNotNil(payload["device_name"])
            requestCompleted.fulfill()
            return .json("{}")
        }

        manager.handleAPNsRegistration(deviceToken: Data([0xde, 0xad, 0xbe, 0xef]))
        await fulfillment(of: [requestCompleted], timeout: 2.0)
        await Task.yield()

        XCTAssertEqual(NotificationBackendURLProtocol.requests.count, 1)
        XCTAssertEqual(UserDefaults.standard.string(forKey: "fa_apns_device_token"), "deadbeef")
        XCTAssertEqual(manager.registrationState, .registered)
        XCTAssertNil(manager.errorMessage)
    }

    func testEnabledAPNsRegistrationReportsServerFailure() async throws {
        seedStoredAuth()
        let authManager = makeAuthManager()
        let manager = NotificationManager()
        manager.bind(authManager: authManager)
        manager.notificationsEnabled = true
        let requestCompleted = expectation(description: "push token registration failed")
        NotificationBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/ios/push-tokens")
            requestCompleted.fulfill()
            return .json(#"{"detail":"push service unavailable"}"#, statusCode: 503)
        }

        manager.handleAPNsRegistration(deviceToken: Data([0xca, 0xfe]))
        await fulfillment(of: [requestCompleted], timeout: 2.0)
        await Task.yield()

        XCTAssertEqual(manager.registrationState, .failed)
        XCTAssertTrue(manager.errorMessage?.contains("503") == true)
        XCTAssertTrue(manager.errorMessage?.contains("push service unavailable") == true)
    }

    func testDisableNotificationsUnregistersStoredTokenAndClearsLocalToken() async throws {
        seedStoredAuth()
        UserDefaults.standard.set("cafebabe", forKey: "fa_apns_device_token")
        let authManager = makeAuthManager()
        let manager = NotificationManager()
        manager.notificationsEnabled = true
        NotificationBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(request.url?.path, "/api/ios/push-tokens/cafebabe")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer notification-api-token")
            return .json("{}")
        }

        await manager.disableNotifications(authManager: authManager)

        XCTAssertEqual(NotificationBackendURLProtocol.requests.count, 1)
        XCTAssertFalse(manager.notificationsEnabled)
        XCTAssertEqual(manager.registrationState, .disabled)
        XCTAssertNil(manager.errorMessage)
        XCTAssertNil(UserDefaults.standard.string(forKey: "fa_apns_device_token"))
    }

    func testApproveActionSubmitsApprovalToConfirmEndpoint() async throws {
        seedStoredAuth()
        let manager = NotificationManager()
        manager.bind(authManager: makeAuthManager())

        let requestCompleted = expectation(description: "confirm_tool POST")
        NotificationBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/confirm_tool")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["request_id"] as? String, "confirm_abc")
            XCTAssertEqual(payload["approved"] as? Bool, true)
            XCTAssertEqual(payload["approving_interface"] as? String, "ios")
            requestCompleted.fulfill()
            return .json(#"{"success":true}"#)
        }

        manager.handleNotificationAction(
            actionIdentifier: "FAMILY_ASSISTANT_APPROVE",
            categoryIdentifier: "FAMILY_ASSISTANT_CONFIRMATION",
            title: "Confirmation needed",
            body: "Create a note?",
            userInfo: ["request_id": "confirm_abc", "conversation_id": "web_conv_1"]
        )

        await fulfillment(of: [requestCompleted], timeout: 2.0)
        XCTAssertNil(manager.pendingConfirmationModal)
    }

    func testDenyActionSubmitsRejectionToConfirmEndpoint() async throws {
        seedStoredAuth()
        let manager = NotificationManager()
        manager.bind(authManager: makeAuthManager())

        let requestCompleted = expectation(description: "confirm_tool POST")
        NotificationBackendURLProtocol.respond { request in
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["request_id"] as? String, "confirm_def")
            XCTAssertEqual(payload["approved"] as? Bool, false)
            requestCompleted.fulfill()
            return .json(#"{"success":true}"#)
        }

        manager.handleNotificationAction(
            actionIdentifier: "FAMILY_ASSISTANT_DENY",
            categoryIdentifier: "FAMILY_ASSISTANT_CONFIRMATION",
            title: "Confirmation needed",
            body: "Create a note?",
            userInfo: ["request_id": "confirm_def"]
        )

        await fulfillment(of: [requestCompleted], timeout: 2.0)
    }

    func testDefaultTapOnConfirmationPresentsModalWithoutNavigating() throws {
        let manager = NotificationManager()

        manager.handleNotificationAction(
            actionIdentifier: UNNotificationDefaultActionIdentifier,
            categoryIdentifier: "FAMILY_ASSISTANT_CONFIRMATION",
            title: "Confirmation needed",
            body: "Create a note for this itinerary?",
            userInfo: ["request_id": "confirm_xyz", "conversation_id": "web_conv_9"]
        )

        let modal = try XCTUnwrap(manager.pendingConfirmationModal)
        XCTAssertEqual(modal.requestID, "confirm_xyz")
        XCTAssertEqual(modal.conversationID, "web_conv_9")
        XCTAssertEqual(modal.body, "Create a note for this itinerary?")
        XCTAssertNil(manager.pendingNavigationPath)
    }

    func testDefaultTapOnMessageNotificationSetsNavigationPathWithoutModal() {
        let manager = NotificationManager()

        manager.handleNotificationAction(
            actionIdentifier: UNNotificationDefaultActionIdentifier,
            categoryIdentifier: "FAMILY_ASSISTANT_MESSAGE",
            title: "New message",
            body: "Hello there",
            userInfo: ["conversation_id": "web_conv_5"]
        )

        XCTAssertNil(manager.pendingConfirmationModal)
        XCTAssertEqual(manager.pendingNavigationPath, "/chat?conversation_id=web_conv_5")
    }

    func testDismissingConfirmationDoesNothing() {
        let manager = NotificationManager()

        manager.handleNotificationAction(
            actionIdentifier: UNNotificationDismissActionIdentifier,
            categoryIdentifier: "FAMILY_ASSISTANT_CONFIRMATION",
            title: "Confirmation needed",
            body: "Create a note?",
            userInfo: ["request_id": "confirm_dismissed"]
        )

        XCTAssertNil(manager.pendingConfirmationModal)
        XCTAssertNil(manager.pendingNavigationPath)
    }

    func testClearPendingConfirmationModalResetsState() {
        let manager = NotificationManager()
        manager.handleNotificationAction(
            actionIdentifier: UNNotificationDefaultActionIdentifier,
            categoryIdentifier: "FAMILY_ASSISTANT_CONFIRMATION",
            title: "Confirmation needed",
            body: "Create a note?",
            userInfo: ["request_id": "confirm_clear"]
        )
        XCTAssertNotNil(manager.pendingConfirmationModal)

        manager.clearPendingConfirmationModal()

        XCTAssertNil(manager.pendingConfirmationModal)
    }

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = "https://assistant.example.test"
        return authManager
    }

    private func seedStoredAuth() {
        KeychainHelper.save(key: "fa_api_token", string: "notification-api-token")
        KeychainHelper.save(key: "fa_refresh_token", string: "notification-refresh-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private func resetStoredNotificationState() {
        UserDefaults.standard.removeObject(forKey: "fa_notifications_enabled")
        UserDefaults.standard.removeObject(forKey: "fa_apns_device_token")
        UserDefaults.standard.removeObject(forKey: "fa_installation_id")
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        let bodyData = try XCTUnwrap(request.httpBody ?? Data(reading: request.httpBodyStream))
        return try JSONSerialization.jsonObject(with: bodyData)
    }
}

private final class NotificationBackendURLProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> NotificationMockResponse

    private static let lock = NSLock()
    private static var handler: Handler?
    private static var recordedRequests: [URLRequest] = []

    static var requests: [URLRequest] {
        lock.withLock { recordedRequests }
    }

    static func respond(with handler: @escaping Handler) {
        lock.withLock {
            self.handler = handler
        }
    }

    static func reset() {
        lock.withLock {
            handler = nil
            recordedRequests = []
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.lock.withLock {
            Self.recordedRequests.append(request)
        }

        guard let handler = Self.lock.withLock({ Self.handler }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        do {
            let response = try handler(request)
            client?.urlProtocol(self, didReceive: response.urlResponse(for: request), cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: response.data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private struct NotificationMockResponse {
    let statusCode: Int
    let data: Data

    static func json(_ json: String, statusCode: Int = 200) -> NotificationMockResponse {
        NotificationMockResponse(statusCode: statusCode, data: Data(json.utf8))
    }

    func urlResponse(for request: URLRequest) -> HTTPURLResponse {
        HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
    }
}

private extension Data {
    init?(reading stream: InputStream?) {
        guard let stream else { return nil }
        self.init()
        stream.open()
        defer { stream.close() }

        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let bytesRead = stream.read(buffer, maxLength: bufferSize)
            if bytesRead < 0 {
                return nil
            }
            append(buffer, count: bytesRead)
        }
    }
}
