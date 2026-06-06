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
    let webViewState: WebViewState

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

    func updateUIView(_ webView: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(state: webViewState)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let state: WebViewState
        private var observation: NSKeyValueObservation?
        private var titleObservation: NSKeyValueObservation?
        private var loadingObservation: NSKeyValueObservation?

        init(state: WebViewState) {
            self.state = state
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
            // Open external links in Safari
            if navigationAction.navigationType == .linkActivated,
               let url = navigationAction.request.url,
               let host = url.host,
               let serverHost = state.webView?.url?.host,
               host != serverHost
            {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
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
