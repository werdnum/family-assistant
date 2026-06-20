import SwiftUI

/// One web-backed destination in the More list.
struct MoreDestination: Identifiable, Hashable {
    var id: String { path }
    let title: String
    let systemImage: String
    let path: String
}

/// A titled group of More destinations.
struct MoreSection: Identifiable, Hashable {
    var id: String { title }
    let title: String
    let destinations: [MoreDestination]
}

/// The long-tail destinations shown under the More tab. Mirrors the canonical
/// web navigation (`frontend/src/shared/navigation.ts`) minus the entries
/// promoted to their own tabs (Chat, Notes, Documents list). Keep this in sync
/// with the web nav; `MoreCatalogTests` guards the path set.
enum MoreCatalog {
    static let sections: [MoreSection] = [
        MoreSection(title: "Data", destinations: [
            MoreDestination(title: "Context", systemImage: "doc.text", path: "/context"),
        ]),
        MoreSection(title: "Documents", destinations: [
            MoreDestination(title: "Upload", systemImage: "arrow.up.doc", path: "/documents/upload"),
            MoreDestination(title: "Search", systemImage: "magnifyingglass", path: "/vector-search"),
        ]),
        MoreSection(title: "Communication", destinations: [
            MoreDestination(title: "Voice", systemImage: "mic", path: "/voice"),
            MoreDestination(title: "History", systemImage: "clock.arrow.circlepath", path: "/history"),
        ]),
        MoreSection(title: "Automation", destinations: [
            MoreDestination(title: "Automations", systemImage: "bolt", path: "/automations"),
            MoreDestination(title: "Events", systemImage: "calendar", path: "/events"),
        ]),
        MoreSection(title: "Internal", destinations: [
            MoreDestination(title: "Tools", systemImage: "wrench.and.screwdriver", path: "/tools"),
            MoreDestination(title: "Task Queue", systemImage: "tray.full", path: "/tasks"),
            MoreDestination(title: "Error Logs", systemImage: "exclamationmark.triangle", path: "/errors"),
        ]),
        MoreSection(title: "Help", destinations: [
            MoreDestination(title: "Help", systemImage: "questionmark.circle", path: "/docs/"),
            MoreDestination(title: "About", systemImage: "info.circle", path: "/about"),
        ]),
    ]
}

struct MoreTabView: View {
    @Bindable var appRouter: AppRouter
    let baseURL: URL
    let onLogout: () -> Void

    var body: some View {
        NavigationStack(path: $appRouter.morePath) {
            List {
                ForEach(MoreCatalog.sections) { section in
                    Section(section.title) {
                        ForEach(section.destinations) { destination in
                            NavigationLink(value: route(for: destination)) {
                                Label(destination.title, systemImage: destination.systemImage)
                            }
                        }
                    }
                }

                Section {
                    NavigationLink(value: MoreRoute.settings) {
                        Label("Settings", systemImage: "gearshape")
                    }
                    .accessibilityIdentifier("more-settings-row")
                }
            }
            .navigationTitle("More")
            .navigationDestination(for: MoreRoute.self) { route in
                switch route {
                case .web(let web):
                    WebDestinationView(
                        path: web.path,
                        baseURL: baseURL,
                        currentTab: .more,
                        fallbackTitle: web.title ?? "More",
                        appRouter: appRouter
                    )
                case .settings:
                    SettingsView(onLogout: onLogout)
                case .voice:
                    VoiceView()
                }
            }
        }
    }

    /// Voice is a native screen; every other catalog entry is web-backed.
    private func route(for destination: MoreDestination) -> MoreRoute {
        if destination.path == "/voice" {
            return .voice
        }
        return .web(WebRoute(path: destination.path, title: destination.title))
    }
}

/// Native settings screen hosting the actions that used to live in the
/// duplicated `AppSettingsMenu`: notification status/toggle and sign-out.
struct SettingsView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager
    let onLogout: () -> Void

    var body: some View {
        Form {
            Section("Notifications") {
                LabeledContent("Status", value: notificationManager.statusLabel)

                if notificationManager.notificationsEnabled {
                    Button {
                        Task {
                            await notificationManager.disableNotifications(authManager: authManager)
                        }
                    } label: {
                        Label("Disable Notifications", systemImage: "bell.slash")
                    }
                } else {
                    Button {
                        Task {
                            await notificationManager.enableNotifications(authManager: authManager)
                        }
                    } label: {
                        Label("Enable Notifications", systemImage: "bell")
                    }
                }

                if notificationManager.authorizationStatus == .denied {
                    Button {
                        notificationManager.openSystemNotificationSettings()
                    } label: {
                        Label("Open iOS Settings", systemImage: "gear")
                    }
                }
            }

            Section {
                Button(role: .destructive, action: onLogout) {
                    Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
                }
                .accessibilityIdentifier("settings-sign-out")
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
    }
}
