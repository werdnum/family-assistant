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
final class StartCallIntentHandler: NSObject, INStartCallIntentHandling {
    func resolveContacts(
        for intent: INStartCallIntent,
        with completion: @escaping ([INStartCallContactResolutionResult]) -> Void
    ) {
        switch AssistantCallHandle.resolveDestination(in: intent.contacts) {
        case .unnamed:
            completion([INStartCallContactResolutionResult.notRequired()])
        case let .assistant(person):
            completion([INStartCallContactResolutionResult.success(with: person)])
        case .unsupported:
            completion([INStartCallContactResolutionResult.unsupported()])
        }
    }

    func handle(intent: INStartCallIntent, completion: @escaping (INStartCallIntentResponse) -> Void) {
        guard AssistantCallHandle.isAddressedToAssistant(intent) else {
            completion(INStartCallIntentResponse(code: .failureContactNotSupportedByApp, userActivity: nil))
            return
        }
        completion(INStartCallIntentResponse(code: .continueInApp, userActivity: nil))
    }
}
