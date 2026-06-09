import AppIntents

/// "Open Family Assistant chat …" — foregrounds the app on the Chat tab. With an
/// optional message it starts a new conversation and auto-sends the prompt.
///
/// `openAppWhenRun` runs this in the app's process and brings it forward; the
/// destination is handed to `ContentView` via `IntentNavigationCenter`.
struct OpenConversationIntent: AppIntent {
    static var title: LocalizedStringResource = "Open Family Assistant Chat"
    static var description = IntentDescription(
        "Open Family Assistant, optionally starting a new chat with a message.",
        categoryName: "Assistant"
    )
    static var openAppWhenRun = true

    @Parameter(title: "Message")
    var prompt: String?

    static var parameterSummary: some ParameterSummary {
        Summary("Open Family Assistant chat with \(\.$prompt)")
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        IntentNavigationCenter.shared.requestChat(prompt: prompt)
        return .result()
    }
}
