import AVFoundation
import UIKit

/// May assistant voice start right now without the device being unlocked?
///
/// Reaching the assistant from a locked phone is the point of the calling
/// surface and also its only new exposure, so the rule lives in one place and
/// every entry point asks it: an entry point that forgets to ask is then the
/// thing that looks wrong rather than the thing that quietly works.
///
/// Access is granted when the device is unlocked, or when the phone is
/// connected to CarPlay — being plugged into the car is the authentication.
/// Ordinary Bluetooth hands-free is a much weaker claim about who is in the car
/// and deliberately does not count.
///
/// Both probes are injected so the rule is testable without a locked device or
/// a car.
struct VoiceHandsFreeAccess {
    static let system = VoiceHandsFreeAccess()

    private let isDeviceUnlocked: @MainActor () -> Bool
    private let isCarPlayConnected: @MainActor () -> Bool

    init(
        isDeviceUnlocked: @escaping @MainActor () -> Bool = VoiceHandsFreeAccess.protectedDataIsAvailable,
        isCarPlayConnected: @escaping @MainActor () -> Bool = VoiceHandsFreeAccess.currentRouteIsCarPlay
    ) {
        self.isDeviceUnlocked = isDeviceUnlocked
        self.isCarPlayConnected = isCarPlayConnected
    }

    /// The rule for an entry point that has no call of its own to inspect, so
    /// has only the app's current audio route to go on.
    @MainActor
    var isAllowed: Bool {
        isDeviceUnlocked() || isCarPlayConnected()
    }

    @MainActor
    var deviceIsUnlocked: Bool {
        isDeviceUnlocked()
    }

    /// The rule as a call applies it. A call's route is not known until
    /// CallKit has activated the call's audio session — before that, the app's
    /// own session is inactive and can report no route at all even in the car
    /// — so the two halves are taken at different moments: whether the device
    /// was unlocked when the call was asked for, and the route the system gave
    /// the call.
    static func admitsCall(unlockedAtRequest: Bool, route: VoiceAudioRoute) -> Bool {
        unlockedAtRequest || route.isCarPlay
    }

    /// There is no API for "is the device unlocked"; protected-data
    /// availability is the conventional proxy, accurate apart from a short
    /// grace period after locking.
    @MainActor
    static func protectedDataIsAvailable() -> Bool {
        UIApplication.shared.isProtectedDataAvailable
    }

    /// An audio-route property, readable without any CarPlay entitlement.
    @MainActor
    static func currentRouteIsCarPlay() -> Bool {
        VoiceAudioRoute(session: .sharedInstance()).isCarPlay
    }
}

/// The port types of an audio route. Types only: a port's name and UID
/// identify the user's car and headphones, and nothing here needs them.
struct VoiceAudioRoute: Equatable {
    var inputs: [AVAudioSession.Port]
    var outputs: [AVAudioSession.Port]
    /// What the session could route input to, which depends on its category.
    /// Recorded, not judged: a port that is available but not in use says
    /// nothing about where the call's audio actually goes.
    var availableInputs: [AVAudioSession.Port]

    init(
        inputs: [AVAudioSession.Port],
        outputs: [AVAudioSession.Port],
        availableInputs: [AVAudioSession.Port] = []
    ) {
        self.inputs = inputs
        self.outputs = outputs
        self.availableInputs = availableInputs
    }

    init(session: AVAudioSession) {
        self.init(
            inputs: session.currentRoute.inputs.map(\.portType),
            outputs: session.currentRoute.outputs.map(\.portType),
            availableInputs: session.availableInputs?.map(\.portType) ?? []
        )
    }

    /// Only `.carAudio` counts, in either direction. The Bluetooth ports are
    /// what an ordinary car pairing looks like, and they are deliberately not
    /// enough.
    var isCarPlay: Bool {
        inputs.contains(.carAudio) || outputs.contains(.carAudio)
    }

    var telemetryFields: [String: String] {
        [
            "route_inputs": Self.describe(inputs),
            "route_outputs": Self.describe(outputs),
            "available_inputs": Self.describe(availableInputs),
            "route_is_carplay": String(isCarPlay),
        ]
    }

    private static func describe(_ ports: [AVAudioSession.Port]) -> String {
        ports.isEmpty ? "none" : ports.map(\.rawValue).joined(separator: ",")
    }
}
