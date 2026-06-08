import SwiftUI

/// Result snippet shown by Siri / the Shortcuts app after an inline assistant
/// reply. Displays the reply text and a "Continue in app" link that deep-links
/// into the originating conversation so the user can keep going (including any
/// tool approvals that cannot be handled inside a snippet).
struct AssistantReplySnippet: View {
    let reply: String
    let conversationID: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(reply)
                .font(.body)
                .frame(maxWidth: .infinity, alignment: .leading)

            Link(destination: IntentSupport.chatDeepLink(conversationID: conversationID)) {
                Label("Continue in app", systemImage: "arrow.up.forward.app")
                    .font(.callout.weight(.semibold))
            }
        }
        .padding()
    }
}
