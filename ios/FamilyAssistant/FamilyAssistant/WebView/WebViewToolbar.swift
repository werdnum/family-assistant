import SwiftUI

struct WebViewToolbar: View {
    let webViewState: WebViewState
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

            Menu {
                Button(role: .destructive, action: onLogout) {
                    Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
                }
            } label: {
                Image(systemName: "gearshape")
            }
        }
        .padding(.horizontal, 32)
        .padding(.vertical, 8)
    }
}
