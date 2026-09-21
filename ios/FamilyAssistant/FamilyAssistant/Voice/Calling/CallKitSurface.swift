import AVFoundation
import CallKit
import Foundation

/// The CallKit action operations the coordinator performs. Wrapped so the state
/// machine can be driven in tests without a live provider handing out real
/// `CXAction`s.
@MainActor
protocol CallAction: AnyObject {
    func fulfill()
    func fail()
}

extension CXAction: CallAction {}

/// Call-state reporting, as much of `CXProvider` as this app uses.
@MainActor
protocol CallProviding: AnyObject {
    func reportOutgoingCall(with uuid: UUID, startedConnectingAt: Date?)
    func reportOutgoingCall(with uuid: UUID, connectedAt: Date?)
    func reportCall(with uuid: UUID, updated update: CXCallUpdate)
    func reportCall(with uuid: UUID, endedAt: Date?, reason: CXCallEndedReason)
    func invalidate()
}

/// Requesting call transactions, as much of `CXCallController` as this app uses.
@MainActor
protocol CallRequesting: AnyObject {
    func requestStartCall(uuid: UUID, handle: CXHandle) async throws
    func requestEndCall(uuid: UUID) async throws
}

/// The provider callbacks the coordinator answers. Keeping this a protocol lets
/// the CallKit-facing delegate stay a thin forwarder with no state of its own.
@MainActor
protocol VoiceCallEventHandling: AnyObject {
    func performStartCall(uuid: UUID, action: any CallAction)
    func performEndCall(uuid: UUID, action: any CallAction)
    func performSetMuted(uuid: UUID, muted: Bool, action: any CallAction)
    /// `route` is read from the session CallKit hands over, at the moment it
    /// hands it over: it is the first point at which the call's route exists.
    func audioSessionActivated(route: VoiceAudioRoute)
    func audioSessionDeactivated()
    func callProviderDidReset()
}

/// Waits a bounded time for a call's audio to reach the car.
@MainActor
protocol CallRouteSettling {
    /// The first route seen that is CarPlay, or the route as it stands once
    /// `timeout` has passed.
    func carPlayRoute(within timeout: Duration) async -> VoiceAudioRoute
}

/// Polled rather than observed: a route settles through route changes and
/// through ports becoming available, and a bounded poll catches both without
/// racing a notification subscription against the deadline.
@MainActor
struct SystemCallRouteSettling: CallRouteSettling {
    /// Holds nothing, so it can be built anywhere — including as a default
    /// argument, which is evaluated outside the main actor.
    nonisolated init() {}

    func carPlayRoute(within timeout: Duration) async -> VoiceAudioRoute {
        let clock = ContinuousClock()
        let deadline = clock.now + timeout
        var route = VoiceAudioRoute(session: .sharedInstance())
        while !route.isCarPlay, clock.now < deadline {
            do {
                try await Task.sleep(for: .milliseconds(100))
            } catch {
                break
            }
            route = VoiceAudioRoute(session: .sharedInstance())
        }
        return route
    }
}

/// The real `CXProvider`, plus the delegate that forwards its callbacks to the
/// coordinator.
@MainActor
final class SystemCallProvider: NSObject, CallProviding {
    weak var handler: (any VoiceCallEventHandling)?

    private let provider: CXProvider

    /// One call at a time, audio only, addressed by a generic handle rather than
    /// a phone number or an address-book entry. Kept out of Recents: a
    /// conversation with the assistant is not a call anyone wants in their call
    /// history, and a call refused on a locked phone even less so.
    ///
    /// `CXProviderConfiguration()` takes its localized name from the bundle's
    /// display name, which is already localized per app language; the
    /// `localizedName:` initializer is deprecated.
    static func makeConfiguration() -> CXProviderConfiguration {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        configuration.includesCallsInRecents = false
        return configuration
    }

    override init() {
        provider = CXProvider(configuration: Self.makeConfiguration())
        super.init()
        provider.setDelegate(self, queue: nil)
    }

    func reportOutgoingCall(with uuid: UUID, startedConnectingAt: Date?) {
        provider.reportOutgoingCall(with: uuid, startedConnectingAt: startedConnectingAt)
    }

    func reportOutgoingCall(with uuid: UUID, connectedAt: Date?) {
        provider.reportOutgoingCall(with: uuid, connectedAt: connectedAt)
    }

    func reportCall(with uuid: UUID, updated update: CXCallUpdate) {
        provider.reportCall(with: uuid, updated: update)
    }

    func reportCall(with uuid: UUID, endedAt: Date?, reason: CXCallEndedReason) {
        provider.reportCall(with: uuid, endedAt: endedAt, reason: reason)
    }

    func invalidate() {
        provider.invalidate()
    }
}

extension SystemCallProvider: CXProviderDelegate {
    nonisolated func providerDidReset(_: CXProvider) {
        MainActor.assumeIsolated { handler?.callProviderDidReset() }
    }

    nonisolated func provider(_: CXProvider, perform action: CXStartCallAction) {
        MainActor.assumeIsolated { handler?.performStartCall(uuid: action.callUUID, action: action) }
    }

    nonisolated func provider(_: CXProvider, perform action: CXEndCallAction) {
        MainActor.assumeIsolated { handler?.performEndCall(uuid: action.callUUID, action: action) }
    }

    nonisolated func provider(_: CXProvider, perform action: CXSetMutedCallAction) {
        MainActor.assumeIsolated {
            handler?.performSetMuted(uuid: action.callUUID, muted: action.isMuted, action: action)
        }
    }

    nonisolated func provider(_: CXProvider, didActivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated {
            handler?.audioSessionActivated(route: VoiceAudioRoute(session: audioSession))
        }
    }

    nonisolated func provider(_: CXProvider, didDeactivate _: AVAudioSession) {
        MainActor.assumeIsolated { handler?.audioSessionDeactivated() }
    }
}

@MainActor
final class SystemCallController: CallRequesting {
    private let controller = CXCallController()

    func requestStartCall(uuid: UUID, handle: CXHandle) async throws {
        try await request(CXStartCallAction(call: uuid, handle: handle))
    }

    func requestEndCall(uuid: UUID) async throws {
        try await request(CXEndCallAction(call: uuid))
    }

    private func request(_ action: CXAction) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            controller.request(CXTransaction(action: action)) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }
}
