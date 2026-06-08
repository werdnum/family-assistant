import AppIntents

/// Pre-registers Siri phrases for the app's intents so they work without the user
/// building a Shortcut first. Every phrase must contain the `\(.applicationName)`
/// token. The app name and its "Family Assistant" App Store/display name resolve
/// from the bundle, so users can also say their own custom invocation name.
struct FamilyAssistantAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskAssistantIntent(),
            phrases: [
                "Ask \(.applicationName)",
                "Ask \(.applicationName) a question",
                "Tell \(.applicationName)",
            ],
            shortTitle: "Ask",
            systemImageName: "bubble.left.and.bubble.right"
        )
        AppShortcut(
            intent: QuickCaptureIntent(),
            phrases: [
                "Capture this in \(.applicationName)",
                "Save this to \(.applicationName)",
            ],
            shortTitle: "Quick Capture",
            systemImageName: "tray.and.arrow.down"
        )
        AppShortcut(
            intent: CreateNoteIntent(),
            phrases: [
                "Add a note to \(.applicationName)",
                "Create a note in \(.applicationName)",
            ],
            shortTitle: "Create Note",
            systemImageName: "note.text"
        )
        AppShortcut(
            intent: OpenConversationIntent(),
            phrases: [
                "Open \(.applicationName) chat",
                "Start a chat in \(.applicationName)",
            ],
            shortTitle: "Open Chat",
            systemImageName: "message"
        )
    }
}
