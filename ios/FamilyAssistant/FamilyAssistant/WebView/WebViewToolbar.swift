import SwiftUI
import UserNotifications

struct WebViewToolbar: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager

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
                Section("Notifications") {
                    Text(notificationManager.statusLabel)

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
                }
            } label: {
                Image(systemName: "gearshape")
            }
        }
        .padding(.horizontal, 32)
        .padding(.vertical, 8)
    }
}
