import Foundation
import Intents

/// How the assistant is addressed — on the system call screen, in the donation
/// Siri resolves a spoken name against, and in the intent that comes back — and
/// the rule for deciding whether a start-call intent is addressed to it.
///
/// Both the app and the Intents extension compile this. The extension declares
/// support for the calling intent and resolves the destination; the app places
/// the call. The two have to agree on one string and one predicate, so the
/// string and the predicate live in one place rather than in each of them.
enum AssistantCallHandle {
    static let value = "Family Assistant"

    /// What a spoken destination resolves to.
    enum Destination: Equatable {
        /// The intent named nobody, so there is nothing to resolve: Siri
        /// resolved this app itself as the destination.
        case unnamed
        /// The intent named the assistant, which is the only thing this app can
        /// call, so that is what the destination narrows to.
        case assistant(INPerson)
        /// The intent named somebody else. Answering that with an assistant
        /// conversation would discard the destination the user asked for.
        case unsupported
    }

    /// Siri resolves "call Bob using Family Assistant" into a start-call intent
    /// for this app too, with Bob as the destination. An intent that names
    /// contacts must therefore name ours.
    ///
    /// An intent naming nobody is accepted: the payload shape on the paths that
    /// matter — a locked phone, a CarPlay head unit — is not something that can
    /// be verified from here, so refusing on a missing payload would break the
    /// primary path to guard a case that has not been seen.
    static func resolveDestination(in contacts: [INPerson]?) -> Destination {
        guard let contacts, !contacts.isEmpty else { return .unnamed }
        guard let assistant = contacts.first(where: isAssistant) else { return .unsupported }
        return .assistant(assistant)
    }

    /// Whether a start-call intent — or an activity carrying no intent payload
    /// at all, which arrives here as `nil` — is one this app should answer by
    /// talking to the assistant.
    static func isAddressedToAssistant(_ intent: INStartCallIntent?) -> Bool {
        resolveDestination(in: intent?.contacts) != .unsupported
    }

    static func isAssistant(_ person: INPerson) -> Bool {
        let names = [person.personHandle?.value, person.customIdentifier, person.displayName]
        return names.contains { name in
            name?.trimmingCharacters(in: .whitespacesAndNewlines)
                .caseInsensitiveCompare(value) == .orderedSame
        }
    }
}
