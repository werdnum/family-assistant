import Intents
import os

/// Teaches Siri that "Family Assistant" is a thing this app can call.
///
/// Siri resolves a spoken destination against donated interactions, so without
/// a donation "call Family Assistant" has nothing to match. The name is the
/// app's own rather than a bare "Assistant", which collides with a common noun
/// and with every other assistant on the phone.
/// The assistant is donated as one constant generic handle rather than written
/// into the user's contacts, which keeps resolution working without taking a
/// write dependency on the address book.
enum AssistantCallDonation {
    /// The same handle the call screen shows, so what Siri hears and what the
    /// user sees while talking are one string.
    static let handleValue = VoiceCallCoordinator.assistantHandleValue

    private static let logger = Logger(subsystem: "com.familyassistant.app", category: "voice-call")

    /// Ask for Siri authorization and donate the callable handle. Called when
    /// the user opens the Voice tab: a cold-launch prompt would be asking for
    /// something the user has not yet shown any interest in.
    @MainActor
    static func prepare(isSignedIn: Bool) {
        guard isSignedIn else { return }
        requestSiriAuthorizationIfNeeded()
        donateStartCall()
    }

    @MainActor
    private static func requestSiriAuthorizationIfNeeded() {
        guard INPreferences.siriAuthorizationStatus() == .notDetermined else { return }
        INPreferences.requestSiriAuthorization { status in
            logger.info("Siri authorization status: \(status.rawValue, privacy: .public)")
        }
    }

    private static func donateStartCall() {
        let interaction = INInteraction(intent: makeStartCallIntent(), response: nil)
        interaction.direction = .outgoing
        interaction.donate { error in
            if let error {
                logger.error("Failed to donate the assistant call handle: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    static func makeStartCallIntent() -> INStartCallIntent {
        let person = INPerson(
            personHandle: INPersonHandle(value: handleValue, type: .unknown),
            nameComponents: nil,
            displayName: handleValue,
            image: nil,
            contactIdentifier: nil,
            customIdentifier: handleValue
        )
        return INStartCallIntent(
            callRecordFilter: nil,
            callRecordToCallBack: nil,
            audioRoute: .unknown,
            destinationType: .normal,
            contacts: [person],
            callCapability: .audioCall
        )
    }
}
