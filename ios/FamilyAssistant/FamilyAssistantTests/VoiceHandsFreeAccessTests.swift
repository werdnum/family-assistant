import AVFoundation
@testable import FamilyAssistant
import Foundation
import XCTest

@MainActor
final class VoiceHandsFreeAccessTests: XCTestCase {
    private func access(unlocked: Bool, carPlay: Bool) -> VoiceHandsFreeAccess {
        VoiceHandsFreeAccess(isDeviceUnlocked: { unlocked }, isCarPlayConnected: { carPlay })
    }

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
    /// the car than a CarPlay connection, so the route probe must not treat it
    /// as one.
    func testBluetoothAloneIsNotCarPlay() {
        let bluetoothPorts: [AVAudioSession.Port] = [.bluetoothHFP, .bluetoothA2DP, .bluetoothLE]
        XCTAssertFalse(VoiceHandsFreeAccess.isCarPlay(outputPortTypes: bluetoothPorts))

        let bluetoothOnly = VoiceHandsFreeAccess(
            isDeviceUnlocked: { false },
            isCarPlayConnected: { VoiceHandsFreeAccess.isCarPlay(outputPortTypes: bluetoothPorts) }
        )
        XCTAssertFalse(bluetoothOnly.isAllowed)
    }

    func testACarPlayOutputRouteIsRecognized() {
        XCTAssertTrue(VoiceHandsFreeAccess.isCarPlay(outputPortTypes: [.bluetoothA2DP, .carAudio]))
    }

    func testNoOutputRouteIsNotCarPlay() {
        XCTAssertFalse(VoiceHandsFreeAccess.isCarPlay(outputPortTypes: []))
    }
}
