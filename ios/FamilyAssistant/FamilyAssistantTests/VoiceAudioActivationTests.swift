@testable import FamilyAssistant
import Foundation
import XCTest

final class VoiceAudioActivationTests: XCTestCase {
    func testPolicyReportsWhoOwnsActivation() {
        let signal = VoiceAudioActivationSignal()
        XCTAssertTrue(VoiceAudioActivationPolicy.selfManaged.isSelfManaged)
        XCTAssertNil(VoiceAudioActivationPolicy.selfManaged.externalSignal)
        XCTAssertFalse(VoiceAudioActivationPolicy.externallyManaged(signal).isSelfManaged)
        XCTAssertTrue(VoiceAudioActivationPolicy.externallyManaged(signal).externalSignal === signal)
    }

    func testSignalBeforeWaitDoesNotLoseTheWakeup() async throws {
        let signal = VoiceAudioActivationSignal()
        signal.signalActivated()
        try await signal.waitForActivation()
        XCTAssertTrue(signal.isActivated)
    }

    func testWaitSuspendsUntilSignalled() async throws {
        let signal = VoiceAudioActivationSignal()
        let resumed = Resumed()

        let waiter = Task {
            try await signal.waitForActivation()
            resumed.mark()
        }

        for _ in 0 ..< 10 {
            await Task.yield()
        }
        XCTAssertFalse(resumed.value, "The wait resumed before the session was activated.")

        signal.signalActivated()
        try await waiter.value
        XCTAssertTrue(resumed.value)
    }

    func testMultipleWaitersAllResume() async throws {
        let signal = VoiceAudioActivationSignal()
        let waiters = (0 ..< 3).map { _ in Task { try await signal.waitForActivation() } }
        for _ in 0 ..< 10 {
            await Task.yield()
        }
        signal.signalActivated()
        for waiter in waiters {
            try await waiter.value
        }
    }

    func testDeactivationMakesTheNextWaitBlockAgain() async throws {
        let signal = VoiceAudioActivationSignal()
        signal.signalActivated()
        try await signal.waitForActivation()

        signal.signalDeactivated()
        XCTAssertFalse(signal.isActivated)

        let resumed = Resumed()
        let waiter = Task {
            try await signal.waitForActivation()
            resumed.mark()
        }
        for _ in 0 ..< 10 {
            await Task.yield()
        }
        XCTAssertFalse(resumed.value)
        signal.signalActivated()
        try await waiter.value
        XCTAssertTrue(resumed.value)
    }

    func testCancellingAParkedWaitThrows() async {
        let signal = VoiceAudioActivationSignal()
        let waiter = Task {
            try await signal.waitForActivation()
        }
        for _ in 0 ..< 10 {
            await Task.yield()
        }
        waiter.cancel()
        do {
            try await waiter.value
            XCTFail("Expected the cancelled wait to throw.")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
    }

    func testWaitOnAnAlreadyCancelledTaskThrows() async {
        let signal = VoiceAudioActivationSignal()
        let waiter = Task {
            // Cancellation may land before the continuation is installed; the
            // wait must still unblock rather than park forever.
            try await signal.waitForActivation()
        }
        waiter.cancel()
        do {
            try await waiter.value
            XCTFail("Expected the cancelled wait to throw.")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
    }
}

/// A cross-task flag for observing whether a suspended wait has resumed.
private final class Resumed: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    var value: Bool {
        lock.withLock { flag }
    }

    func mark() {
        lock.withLock { flag = true }
    }
}
