import SwiftUI

struct WebViewToolbar: View {
    let webViewState: WebViewState
    let onShowNotes: () -> Void
    let onLogout: () -> Void

    var body: some View {
        HStack {
            Button(action: { webViewState.goBack() }) {
                Image(systemName: "chevron.left")
            }
            .disabled(!webViewState.canGoBack)

            Spacer()

            Button(action: { webViewState.goForward() }) {
                Image(systemName: "chevron.right")
            }
            .disabled(!webViewState.canGoForward)

            Spacer()

            Button(action: { webViewState.reload() }) {
                Image(systemName: "arrow.clockwise")
            }

            Spacer()

            Button(action: onShowNotes) {
                Image(systemName: "note.text")
            }

            Spacer()

            AppSettingsMenu(onLogout: onLogout)
        }
        .padding(.horizontal, 32)
        .padding(.vertical, 8)
    }
}
