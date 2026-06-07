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
        config.userContentController.addUserScript(Self.nativeNavigationScript)
        config.userContentController.add(context.coordinator, name: Self.nativeNavigationMessageName)

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
        uiView.configuration.userContentController.removeScriptMessageHandler(
            forName: Self.nativeNavigationMessageName
        )
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

    private static let nativeNavigationMessageName = "familyAssistantNativeNavigation"

    private static let nativeNavigationScript = WKUserScript(
        source: """
        (() => {
          if (window.__familyAssistantNativeNavigationInstalled) {
            return;
          }
          window.__familyAssistantNativeNavigationInstalled = true;

          const postLocation = () => {
            try {
              window.webkit.messageHandlers.familyAssistantNativeNavigation.postMessage(window.location.href);
            } catch (_) {
              // WebKit may not expose the bridge in every loading context.
            }
          };

          const postSoon = () => window.setTimeout(postLocation, 0);
          const originalPushState = window.history.pushState;
          const originalReplaceState = window.history.replaceState;

          window.history.pushState = function() {
            const result = originalPushState.apply(this, arguments);
            postSoon();
            return result;
          };

          window.history.replaceState = function() {
            const result = originalReplaceState.apply(this, arguments);
            postSoon();
            return result;
          };

          window.addEventListener("popstate", postSoon);
        })();
        """,
        injectionTime: .atDocumentEnd,
        forMainFrameOnly: true
    )

    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
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

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == WebViewContainer.nativeNavigationMessageName,
                  let value = message.body as? String,
                  let url = URL(string: value)
            else {
                return
            }

            if handleInternalNavigation(url) {
                state.webView?.stopLoading()
            }
        }

        private func updateState(_ webView: WKWebView) {
            state.canGoBack = webView.canGoBack
            state.canGoForward = webView.canGoForward
            state.title = webView.title ?? ""
            state.isLoading = webView.isLoading
        }

        private func handleInternalNavigation(_ url: URL) -> Bool {
            guard url.scheme == "http" || url.scheme == "https",
                  url.matchesOrigin(of: serverBaseURL)
            else {
                return false
            }
            return onInternalNavigation(url)
        }
    }
}
