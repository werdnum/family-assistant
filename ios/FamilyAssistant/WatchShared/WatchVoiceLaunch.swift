import Foundation

enum WatchVoiceLaunch {
    static let url = URL(string: "familyassistant-watch://voice")!

    static func opensVoice(_ url: URL) -> Bool {
        url.scheme == "familyassistant-watch" && url.host == "voice" && url.path.isEmpty
    }
}
