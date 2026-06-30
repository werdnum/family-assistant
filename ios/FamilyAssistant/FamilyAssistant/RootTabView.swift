import SwiftUI

/// The app's root: a four-tab `TabView` whose tabs are features, not
/// implementations. Chat, Voice, and Notes render native screens; Documents
/// and More render focused web pages inside their own navigation stacks. Each
/// tab keeps independent navigation state across tab switches via `AppRouter`.
struct RootTabView: View {
    @Bindable var appRouter: AppRouter
    let authManager: AuthManager
    let baseURL: URL
    let onLogout: () -> Void

    var body: some View {
        TabView(selection: $appRouter.selectedTab) {
            ChatRootView(
                authManager: authManager,
                route: appRouter.chatSelection
            )
            .tabItem { Label("Chat", systemImage: "message") }
            .tag(AppTab.chat)

            NavigationStack {
                VoiceView()
            }
            .tabItem { Label("Voice", systemImage: "mic") }
            .tag(AppTab.voice)

            NotesRootView(
                route: appRouter.notesRoute,
                onRouteChange: { appRouter.notesRoute = $0 }
            )
            .tabItem { Label("Notes", systemImage: "note.text") }
            .tag(AppTab.notes)

            DocumentsTabView(appRouter: appRouter, baseURL: baseURL)
                .tabItem { Label("Documents", systemImage: "folder") }
                .tag(AppTab.documents)

            MoreTabView(appRouter: appRouter, baseURL: baseURL, onLogout: onLogout)
                .tabItem { Label("More", systemImage: "ellipsis") }
                .tag(AppTab.more)
        }
    }
}

/// The Documents tab: the web `/documents/` page as its root, with deeper
/// document paths pushed onto the tab's navigation stack.
struct DocumentsTabView: View {
    @Bindable var appRouter: AppRouter
    let baseURL: URL

    var body: some View {
        NavigationStack(path: $appRouter.documentsPath) {
            WebDestinationView(
                path: "/documents/",
                baseURL: baseURL,
                currentTab: .documents,
                fallbackTitle: "Documents",
                appRouter: appRouter
            )
            .navigationDestination(for: WebRoute.self) { route in
                WebDestinationView(
                    path: route.path,
                    baseURL: baseURL,
                    currentTab: .documents,
                    fallbackTitle: route.title ?? "Documents",
                    appRouter: appRouter
                )
            }
        }
    }
}
