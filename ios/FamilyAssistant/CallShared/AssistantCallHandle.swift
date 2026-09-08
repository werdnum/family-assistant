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
        /// The intent named the assistant, and nobody else.
        case assistant(INPerson)
        /// The intent named somebody else, or named more people than a call to
        /// the assistant has room for.
        case unsupported
    }

    /// An assistant call has exactly one destination — the provider advertises
    /// one call per group — so a request naming anyone besides the assistant,
    /// or naming the assistant alongside somebody else, is not one this app can
    /// serve. Siri resolves "call Bob using Family Assistant" to this app too,
    /// and answering that with an assistant conversation would discard the
    /// destination the user asked for.
    ///
    /// An intent naming nobody is accepted: the payload shape on the paths that
    /// matter — a locked phone, a CarPlay head unit — is not something that can
    /// be verified from here, so refusing on a missing payload would break the
    /// primary path to guard a case that has not been seen.
    static func resolveDestination(in contacts: [INPerson]?) -> Destination {
        guard let contacts, !contacts.isEmpty else { return .unnamed }
        guard contacts.count == 1, let person = contacts.first, isAssistant(person) else {
            return .unsupported
        }
        return .assistant(person)
    }

    /// The same decision in the shape SiriKit's resolution callback expects:
    /// one result per contact the intent named, positionally. An intent that
    /// named nobody gets the single `.notRequired()` that stands for an absent
    /// parameter.
    static func resolveContacts(in contacts: [INPerson]?) -> [INStartCallContactResolutionResult] {
        switch resolveDestination(in: contacts) {
        case .unnamed:
            return [INStartCallContactResolutionResult.notRequired()]
        case let .assistant(person):
            return [INStartCallContactResolutionResult.success(with: person)]
        case .unsupported:
            return (contacts ?? []).map { _ in INStartCallContactResolutionResult.unsupported() }
        }
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
