import SwiftUI

struct AppSettingsMenu: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager

    let onLogout: () -> Void

    var body: some View {
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
}
