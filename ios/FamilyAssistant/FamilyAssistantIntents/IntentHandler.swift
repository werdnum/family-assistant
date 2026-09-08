import Intents

/// The extension's principal class, named by `NSExtensionPrincipalClass`.
///
/// `IntentsSupported` declares exactly one intent, so there is exactly one
/// handler to hand back.
final class IntentHandler: INExtension {
    override func handler(for _: INIntent) -> Any {
        StartCallIntentHandler()
    }
}
