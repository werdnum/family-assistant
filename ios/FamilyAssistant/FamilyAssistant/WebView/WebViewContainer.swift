import SwiftUI
import WebKit

@Observable
final class WebViewState {
    var canGoBack = false
    var canGoForward = false
    var title: String = ""
    var isLoading = true

    fileprivate var webView: WKWebView?
    fileprivate var pendingURL: URL?

    func goBack() { webView?.goBack() }
    func goForward() { webView?.goForward() }
    func reload() { webView?.reload() }
    func load(_ url: URL) {
        if let webView {
            webView.load(URLRequest(url: url))
        } else {
            pendingURL = url
        }
    }
}

struct WebViewContainer: UIViewRepresentable {
    let url: URL
    let serverBaseURL: URL
    let webViewState: WebViewState
    let onInternalNavigation: (URL) -> Bool

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true

        // Pull-to-refresh
        let refreshControl = UIRefreshControl()
        refreshControl.addTarget(
            context.coordinator,
            action: #selector(Coordinator.handleRefresh(_:)),
            for: .valueChanged
        )
        webView.scrollView.refreshControl = refreshControl

        webViewState.webView = webView
        let requestURL = webViewState.pendingURL ?? url
        webViewState.pendingURL = nil
        webView.load(URLRequest(url: requestURL))

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.serverBaseURL = serverBaseURL
        context.coordinator.onInternalNavigation = onInternalNavigation
    }

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        if coordinator.state.webView === uiView {
            coordinator.state.webView = nil
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(
            state: webViewState,
            serverBaseURL: serverBaseURL,
            onInternalNavigation: onInternalNavigation
        )
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let state: WebViewState
        var serverBaseURL: URL
        var onInternalNavigation: (URL) -> Bool
        private var observation: NSKeyValueObservation?
        private var titleObservation: NSKeyValueObservation?
        private var loadingObservation: NSKeyValueObservation?

        init(
            state: WebViewState,
            serverBaseURL: URL,
            onInternalNavigation: @escaping (URL) -> Bool
        ) {
            self.state = state
            self.serverBaseURL = serverBaseURL
            self.onInternalNavigation = onInternalNavigation
        }

        @objc func handleRefresh(_ refreshControl: UIRefreshControl) {
            state.webView?.reload()
            // End refreshing after a short delay to let the page start loading
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                refreshControl.endRefreshing()
            }
        }

        // Update navigation state when pages load
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            updateState(webView)
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            updateState(webView)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            if navigationAction.navigationType == .linkActivated,
               let url = navigationAction.request.url
            {
                guard url.scheme == "http" || url.scheme == "https" else {
                    UIApplication.shared.open(url)
                    decisionHandler(.cancel)
                    return
                }

                if !url.matchesOrigin(of: serverBaseURL) {
                    UIApplication.shared.open(url)
                    decisionHandler(.cancel)
                    return
                }

                if onInternalNavigation(url) {
                    decisionHandler(.cancel)
                    return
                }
            }
            decisionHandler(.allow)
        }

        private func updateState(_ webView: WKWebView) {
            state.canGoBack = webView.canGoBack
            state.canGoForward = webView.canGoForward
            state.title = webView.title ?? ""
            state.isLoading = webView.isLoading
        }
    }
}
