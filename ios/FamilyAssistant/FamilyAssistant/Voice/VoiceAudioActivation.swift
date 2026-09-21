import Foundation
import os

/// Who owns activation of the shared `AVAudioSession` for a voice session.
///
/// The in-app Voice tab activates the session itself. Under CallKit the system
/// activates it, and audio started before that is silent or fails outright, so
/// the engine must configure its category and then wait to be told the session
/// is live.
enum VoiceAudioActivationPolicy {
    case selfManaged
    case externallyManaged(VoiceAudioActivationSignal)

    var isSelfManaged: Bool {
        if case .selfManaged = self { return true }
        return false
    }

    var externalSignal: VoiceAudioActivationSignal? {
        if case .externallyManaged(let signal) = self { return signal }
        return nil
    }
}

/// The rendezvous between whoever activates the audio session and whoever is
/// waiting to start audio on it.
///
/// Signalling before anyone waits is the normal case, not an edge case: CallKit
/// can call back with an activated session before the session view model has
/// reached its audio start. Activation is therefore latched, so a wait that
/// arrives afterwards returns immediately rather than hanging for a wakeup that
/// already happened.
final class VoiceAudioActivationSignal: Sendable {
    private struct State {
        var isActivated = false
        var waiters: [UUID: CheckedContinuation<Void, Error>] = [:]
        /// Waits whose task was cancelled before the continuation was installed.
        var cancelledBeforeParking: Set<UUID> = []
    }

    private let state = OSAllocatedUnfairLock(initialState: State())

    init() {}

    var isActivated: Bool {
        state.withLock { $0.isActivated }
    }

    func signalActivated() {
        let waiters = state.withLock { current -> [CheckedContinuation<Void, Error>] in
            current.isActivated = true
            let parked = Array(current.waiters.values)
            current.waiters.removeAll()
            return parked
        }
        for waiter in waiters {
            waiter.resume()
        }
    }

    /// The audio session is no longer active. A subsequent wait blocks again
    /// until the next activation.
    func signalDeactivated() {
        state.withLock { $0.isActivated = false }
    }

    /// Suspend until the audio session has been activated. Throws
    /// `CancellationError` if the calling task is cancelled first.
    func waitForActivation() async throws {
        let id = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                enum Outcome { case activated, cancelled, parked }
                let outcome = state.withLock { current -> Outcome in
                    if current.isActivated { return .activated }
                    if current.cancelledBeforeParking.remove(id) != nil { return .cancelled }
                    current.waiters[id] = continuation
                    return .parked
                }
                switch outcome {
                case .activated:
                    continuation.resume()
                case .cancelled:
                    continuation.resume(throwing: CancellationError())
                case .parked:
                    break
                }
            }
        } onCancel: {
            let waiter = state.withLock { current -> CheckedContinuation<Void, Error>? in
                if let parked = current.waiters.removeValue(forKey: id) { return parked }
                current.cancelledBeforeParking.insert(id)
                return nil
            }
            waiter?.resume(throwing: CancellationError())
        }
    }
}
