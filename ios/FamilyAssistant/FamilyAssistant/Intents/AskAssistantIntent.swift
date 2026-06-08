import AppIntents
import SwiftUI

/// "Ask Family Assistant …" — sends a prompt and shows the assistant's reply
/// inline (Siri dialog + snippet), with a "Continue in app" affordance.
struct AskAssistantIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Family Assistant"
    static var description = IntentDescription(
        "Send a question or request to your Family Assistant and get a reply.",
        categoryName: "Assistant"
    )
    /// Runs in the background and presents the reply; the user opens the app only
    /// via the snippet's "Continue in app" link.
    static var openAppWhenRun = false

    @Parameter(
        title: "Message",
        requestValueDialog: "What would you like to ask Family Assistant?"
    )
    var prompt: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Family Assistant \(\.$prompt)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let manager = try IntentSupport.makeAuthenticatedManager()
        let client = ChatAPIClient(authManager: manager)

        let result: ChatSendResult
        do {
            result = try await client.sendMessage(
                prompt: prompt,
                conversationID: IntentSupport.newConversationID(),
                profileID: IntentSupport.defaultProfileID
            )
        } catch let error as AssistantIntentError {
            throw error
        } catch {
            throw AssistantIntentError.requestFailed(error.localizedDescription)
        }

        return .result(
            dialog: IntentDialog(stringLiteral: result.reply),
            view: AssistantReplySnippet(reply: result.reply, conversationID: result.conversationID)
        )
    }
}
