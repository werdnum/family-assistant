import Intents

/// Declares and resolves the calling intent. The call itself stays in the app.
///
/// Declaring support here is what makes Siri offer this app as a call provider
/// at all: without an extension carrying `INStartCallIntent` in
/// `IntentsSupported`, Siri refuses the request before any activity
/// continuation is reached, and `NSUserActivityTypes` in the app is never
/// consulted. Issuing the CallKit transaction from here is the thing that must
/// not happen — an extension holds none of the calling entitlements it needs —
/// so the response is `.continueInApp`, which hands the intent to the app as an
/// `NSUserActivity` for `VoiceCallRequestCenter` to act on.
///
/// The activity is constructed here rather than left to SiriKit. SiriKit will
/// make one when given none, but the type it chooses is not documented, and the
/// app matches arriving activities on exactly one type — a type it assumed
/// rather than set. Setting it makes the app's match a match against something
/// this app decided, and the constant is shared with the app so the two cannot
/// disagree. SiriKit attaches the `INInteraction` carrying the intent and this
/// response to whichever activity it delivers, so nothing is lost by supplying
/// one.
final class StartCallIntentHandler: NSObject, INStartCallIntentHandling {
    func resolveContacts(
        for intent: INStartCallIntent,
        with completion: @escaping ([INStartCallContactResolutionResult]) -> Void
    ) {
        completion(AssistantCallHandle.resolveContacts(in: intent.contacts))
    }

    func handle(intent: INStartCallIntent, completion: @escaping (INStartCallIntentResponse) -> Void) {
        guard AssistantCallHandle.isAddressedToAssistant(intent) else {
            completion(INStartCallIntentResponse(code: .failureContactNotSupportedByApp, userActivity: nil))
            return
        }
        let activity = NSUserActivity(activityType: AssistantCallHandle.startCallActivityType)
        completion(INStartCallIntentResponse(code: .continueInApp, userActivity: activity))
    }
}
