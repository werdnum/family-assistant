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
    private static let logger = Logger(subsystem: "com.familyassistant.app", category: "voice-call")

    /// Ask for Siri authorization. Called when the user opens the Voice tab: it
    /// is the only part of this that prompts, and a cold-launch prompt would be
    /// asking for something the user has not yet shown any interest in.
    @MainActor
    static func requestSiriAuthorizationIfNeeded() {
        guard INPreferences.siriAuthorizationStatus() == .notDetermined else { return }
        INPreferences.requestSiriAuthorization { status in
            logger.info("Siri authorization status: \(status.rawValue, privacy: .public)")
        }
    }

    static func donate(_ interaction: INInteraction) {
        interaction.donate { error in
            if let error {
                logger.error(
                    "Failed to donate the assistant call handle: \(error.localizedDescription, privacy: .public)"
                )
            }
        }
    }

    static func makeInteraction() -> INInteraction {
        let interaction = INInteraction(intent: makeStartCallIntent(), response: nil)
        interaction.direction = .outgoing
        return interaction
    }

    static func makeStartCallIntent() -> INStartCallIntent {
        let person = INPerson(
            personHandle: INPersonHandle(value: AssistantCallHandle.value, type: .unknown),
            nameComponents: nil,
            displayName: AssistantCallHandle.value,
            image: nil,
            contactIdentifier: nil,
            customIdentifier: AssistantCallHandle.value
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

/// Donates the callable handle as soon as the app is signed in.
///
/// Signing in is the only prerequisite the user is told about, so the donation
/// cannot wait for the Voice tab to be opened: a user who never opens it would
/// otherwise have nothing for Siri to resolve. It is therefore driven from app
/// scope, which covers a launch that is already signed in and a sign-in that
/// happens later in the same session. Donating prompts for nothing and needs no
/// authorization, which is what makes doing it unasked correct; requesting Siri
/// authorization, which does prompt, stays with the Voice tab.
@MainActor
final class AssistantCallDonor {
    private let donate: (INInteraction) -> Void
    private var hasDonated = false

    init(donate: @escaping (INInteraction) -> Void = AssistantCallDonation.donate) {
        self.donate = donate
    }

    /// The handle is a constant, so one donation per process says everything
    /// there is to say. Every place that learns the signed-in state calls this,
    /// and the first signed-in answer is the one that donates.
    func signedInStateChanged(to isSignedIn: Bool) {
        guard isSignedIn, !hasDonated else { return }
        hasDonated = true
        donate(AssistantCallDonation.makeInteraction())
    }
}
