import SwiftUI

/// A focused web page rendered inside a feature tab's `NavigationStack`.
///
/// Wraps `WebViewContainer` with its own `WebViewState`, a navigation title
/// driven by the page (falling back to a caller-supplied label), and same-origin
/// link interception that pushes within the current tab via the `AppRouter`.
/// System back (the navigation stack) and pull-to-refresh replace the old
/// bottom `WebViewToolbar`.
struct WebDestinationView: View {
    let path: String
    let baseURL: URL
    let currentTab: AppTab
    let fallbackTitle: String
    let appRouter: AppRouter

    @State private var webViewState = WebViewState()

    private var url: URL {
        URL(string: path, relativeTo: baseURL)?.absoluteURL ?? baseURL
    }

    var body: some View {
        WebViewContainer(
            url: url,
            serverBaseURL: baseURL,
            webViewState: webViewState
        ) { tappedURL in
            appRouter.followWebLink(tappedURL, from: currentTab, relativeTo: baseURL)
        }
        .navigationTitle(webViewState.title.isEmpty ? fallbackTitle : webViewState.title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
