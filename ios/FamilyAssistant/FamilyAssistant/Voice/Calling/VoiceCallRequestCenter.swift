import Foundation
import Observation

/// One delivered "call the assistant" request. Carries an identity only so a
/// second request while one is still pending is a distinct element rather than
/// a silently collapsed duplicate.
struct VoiceCallRequest: Equatable, Identifiable {
    let id: UUID

    init(id: UUID = UUID()) {
        self.id = id
    }
}

/// Where the start-call requests Siri delivers arrive, and where the thing that
/// acts on them is installed.
///
/// A start-call intent arrives as an `NSUserActivity`, through the scene
/// delegate when a scene is already connected and through the application
/// delegate when the system launched the app in the background for it. Both
/// hooks land here, and the handler ``FamilyAssistantApp`` installs at launch
/// runs immediately, so a call starts on a background launch with no scene.
///
/// Requests that arrive before the handler is installed are held until it is;
/// that buffer is the narrow window at launch, not the normal path.
@MainActor
@Observable
final class VoiceCallRequestCenter {
    static let shared = VoiceCallRequestCenter()

    private(set) var pendingRequests: [VoiceCallRequest] = []

    @ObservationIgnored private let telemetry: VoiceCallTelemetryRecording
    @ObservationIgnored private var handler: (@MainActor (VoiceCallRequest) -> Void)?

    init(telemetry: VoiceCallTelemetryRecording = VoiceCallTelemetry.shared) {
        self.telemetry = telemetry
    }

    /// Install what acts on requests, draining anything already buffered
    /// through it. Passing `nil` detaches the current handler, which puts the
    /// center back to buffering.
    func installHandler(_ handler: (@MainActor (VoiceCallRequest) -> Void)?) {
        self.handler = handler
        guard let handler else { return }
        for request in consumePendingRequests() {
            handler(request)
        }
    }

    func receiveStartCallRequest() {
        let request = VoiceCallRequest()
        guard let handler else {
            pendingRequests.append(request)
            telemetry.record(
                "iOS received a start-call request",
                component: VoiceCallTelemetryComponent.request,
                extraData: ["disposition": "buffered", "pending_count": String(pendingRequests.count)]
            )
            return
        }
        telemetry.record(
            "iOS received a start-call request",
            component: VoiceCallTelemetryComponent.request,
            extraData: ["disposition": "handled", "pending_count": "0"]
        )
        handler(request)
    }

    /// Returns and clears any buffered requests.
    func consumePendingRequests() -> [VoiceCallRequest] {
        defer { pendingRequests = [] }
        return pendingRequests
    }
}
