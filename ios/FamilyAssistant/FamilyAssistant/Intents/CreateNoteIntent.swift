import AppIntents

/// "Add a note to Family Assistant …" — saves a titled note via the notes API.
struct CreateNoteIntent: AppIntent {
    static var title: LocalizedStringResource = "Create Note in Family Assistant"
    static var description = IntentDescription(
        "Save a note to your Family Assistant.",
        categoryName: "Notes"
    )
    static var openAppWhenRun = false

    @Parameter(title: "Title", requestValueDialog: "What should the note be called?")
    var title: String

    @Parameter(title: "Content", requestValueDialog: "What should the note say?")
    var content: String

    static var parameterSummary: some ParameterSummary {
        Summary("Create note \(\.$title) in Family Assistant") {
            \.$content
        }
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let manager = try IntentSupport.makeAuthenticatedManager()
        let client = NotesAPIClient(authManager: manager)

        do {
            try await client.saveNote(
                NativeNoteSaveRequest(
                    title: title,
                    content: content,
                    includeInPrompt: true,
                    originalTitle: nil,
                    attachmentIds: [],
                    visibilityLabels: []
                )
            )
        } catch let error as AssistantIntentError {
            throw error
        } catch {
            throw AssistantIntentError.requestFailed(error.localizedDescription)
        }

        return .result(dialog: "Saved “\(title)” to your notes.")
    }
}
