import AppIntents
import SwiftUI

/// "Capture in Family Assistant …" — sends text or a link to the assistant and
/// asks it to file the content appropriately. Accepts input passed from a
/// Shortcut (including a Shortcut surfaced in the Share Sheet).
struct QuickCaptureIntent: AppIntent {
    static var title: LocalizedStringResource = "Quick Capture to Family Assistant"
    static var description = IntentDescription(
        "Send text or a link to your Family Assistant to save and file for you.",
        categoryName: "Assistant"
    )
    static var openAppWhenRun = false

    @Parameter(
        title: "Content",
        requestValueDialog: "What would you like to capture?"
    )
    var content: String

    static var parameterSummary: some ParameterSummary {
        Summary("Capture \(\.$content) in Family Assistant")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let result = try await IntentSupport.sendAssistantMessage(
            prompt: IntentSupport.quickCapturePrompt(for: content),
            profileID: IntentSupport.captureProfileID
        )
        return .result(
            dialog: IntentDialog(stringLiteral: result.reply),
            view: AssistantReplySnippet(reply: result.reply, conversationID: result.conversationID)
        )
    }
}
