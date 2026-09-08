import AVFoundation
import UIKit

/// May assistant voice start right now without the device being unlocked?
///
/// Reaching the assistant from a locked phone is the point of the calling
/// surface and also its only new exposure, so one predicate answers the
/// question for every entry point: an entry point that forgets to ask is then
/// the thing that looks wrong rather than the thing that quietly works.
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
        isCarPlayConnected: @escaping @MainActor () -> Bool = VoiceHandsFreeAccess.carPlayIsAnOutputRoute
    ) {
        self.isDeviceUnlocked = isDeviceUnlocked
        self.isCarPlayConnected = isCarPlayConnected
    }

    @MainActor
    var isAllowed: Bool {
        isDeviceUnlocked() || isCarPlayConnected()
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
    static func carPlayIsAnOutputRoute() -> Bool {
        isCarPlay(outputPortTypes: AVAudioSession.sharedInstance().currentRoute.outputs.map(\.portType))
    }

    /// Only `.carAudio` counts. The Bluetooth ports are what an ordinary car
    /// pairing looks like, and they are deliberately not enough.
    static func isCarPlay(outputPortTypes: [AVAudioSession.Port]) -> Bool {
        outputPortTypes.contains(.carAudio)
    }
}
