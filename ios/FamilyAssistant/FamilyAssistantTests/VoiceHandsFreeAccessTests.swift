import AVFoundation
@testable import FamilyAssistant
import Foundation
import XCTest

@MainActor
final class VoiceHandsFreeAccessTests: XCTestCase {
    private func access(unlocked: Bool, carPlay: Bool) -> VoiceHandsFreeAccess {
        VoiceHandsFreeAccess(isDeviceUnlocked: { unlocked }, isCarPlayConnected: { carPlay })
    }

    private static let bluetoothPorts: [AVAudioSession.Port] = [.bluetoothHFP, .bluetoothA2DP, .bluetoothLE]

    func testAnUnlockedDeviceIsAllowed() {
        XCTAssertTrue(access(unlocked: true, carPlay: false).isAllowed)
    }

    func testALockedDeviceOnCarPlayIsAllowed() {
        XCTAssertTrue(access(unlocked: false, carPlay: true).isAllowed)
    }

    func testAnUnlockedDeviceOnCarPlayIsAllowed() {
        XCTAssertTrue(access(unlocked: true, carPlay: true).isAllowed)
    }

    func testALockedDeviceOffCarPlayIsRefused() {
        XCTAssertFalse(access(unlocked: false, carPlay: false).isAllowed)
    }

    /// An ordinary Bluetooth car pairing is a much weaker claim about who is in
    /// the car than a CarPlay connection, so a route made of it is not CarPlay
    /// in either direction.
    func testBluetoothAloneIsNotCarPlay() {
        let route = VoiceAudioRoute(
            inputs: Self.bluetoothPorts,
            outputs: Self.bluetoothPorts,
            availableInputs: Self.bluetoothPorts
        )
        XCTAssertFalse(route.isCarPlay)
        XCTAssertFalse(VoiceHandsFreeAccess.admitsCall(unlockedAtRequest: false, route: route))
    }

    func testACarPlayOutputIsRecognized() {
        XCTAssertTrue(VoiceAudioRoute(inputs: [.builtInMic], outputs: [.bluetoothA2DP, .carAudio]).isCarPlay)
    }

    func testACarPlayInputIsRecognized() {
        XCTAssertTrue(VoiceAudioRoute(inputs: [.carAudio], outputs: [.builtInSpeaker]).isCarPlay)
    }

    /// A car that is connected but not carrying the call — the user has moved
    /// it to headphones, say — is not the call's route, so it is not counted.
    func testCarAudioThatIsOnlyAvailableIsNotCarPlay() {
        let route = VoiceAudioRoute(
            inputs: [.builtInMic],
            outputs: [.builtInReceiver],
            availableInputs: [.carAudio]
        )
        XCTAssertFalse(route.isCarPlay)
    }

    func testNoRouteIsNotCarPlay() {
        XCTAssertFalse(VoiceAudioRoute(inputs: [], outputs: []).isCarPlay)
    }

    func testACallFromAnUnlockedDeviceIsAdmittedOnAnyRoute() {
        let route = VoiceAudioRoute(inputs: [], outputs: [])
        XCTAssertTrue(VoiceHandsFreeAccess.admitsCall(unlockedAtRequest: true, route: route))
    }

    func testACallFromALockedDeviceIsAdmittedOnlyOnCarPlay() {
        let carPlay = VoiceAudioRoute(inputs: [.carAudio], outputs: [.carAudio])
        let speaker = VoiceAudioRoute(inputs: [.builtInMic], outputs: [.builtInSpeaker])
        XCTAssertTrue(VoiceHandsFreeAccess.admitsCall(unlockedAtRequest: false, route: carPlay))
        XCTAssertFalse(VoiceHandsFreeAccess.admitsCall(unlockedAtRequest: false, route: speaker))
    }

    /// The route is recorded as port types, which is what the device test
    /// reads; an empty list is spelled out rather than left blank.
    func testTelemetryFieldsCarryPortTypesOnly() {
        let route = VoiceAudioRoute(inputs: [.carAudio], outputs: [], availableInputs: [.builtInMic, .carAudio])
        XCTAssertEqual(route.telemetryFields, [
            "route_inputs": AVAudioSession.Port.carAudio.rawValue,
            "route_outputs": "none",
            "available_inputs": "\(AVAudioSession.Port.builtInMic.rawValue),\(AVAudioSession.Port.carAudio.rawValue)",
            "route_is_carplay": "true",
        ])
    }
}
