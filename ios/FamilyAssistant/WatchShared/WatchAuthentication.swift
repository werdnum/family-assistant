import Foundation
import WatchConnectivity

struct WatchCredentials: Codable {
    let serverURL: String
    let phoneSessionID: String
    let tokens: TokenResponse
}

/// Credentials use interactive replies only; queued context contains no secrets.
@MainActor
@Observable
final class WatchAuthentication: NSObject, WCSessionDelegate {
    private let auth: AuthManager
    private let session: WCSession?
    private let requestCredentials: ((@escaping @MainActor (Data?, String?) -> Void) -> Void)?
    private(set) var isConnecting = false
    private(set) var message: String?
    private var requestID: UUID?
    private var latestPhoneIdentity: String?
    private let pairedSessionKey = "fa_paired_phone_session"

    init(auth: AuthManager, session: WCSession? = WCSession.isSupported() ? .default : nil,
         requestCredentials: ((@escaping @MainActor (Data?, String?) -> Void) -> Void)? = nil)
    {
        self.auth = auth
        self.session = session
        self.requestCredentials = requestCredentials
        super.init()
    }

    func activate() {
        session?.delegate = self
        session?.activate()
    }

    func publishPhoneState() {
        #if os(iOS)
        guard !auth.isBootstrapping, let session, session.activationState == .activated, session.isPaired, session.isWatchAppInstalled else { return }
        do {
            try session.updateApplicationContext(["phoneSessionID": auth.watchPairingIdentity])
        } catch {
            message = "Could not update Apple Watch. Open both apps to try again."
        }
        #endif
    }

    func connect() {
        guard !isConnecting else { return }
        guard requestCredentials != nil || (session?.activationState == .activated && session?.isReachable == true) else {
            message = "Open Family Assistant on your paired iPhone, sign in, then try again."
            return
        }
        let id = UUID()
        requestID = id
        isConnecting = true
        message = nil
        let identityAtStart = latestPhoneIdentity
        let completion: @MainActor (Data?, String?) -> Void = { data, error in
            self.receiveReply(id: id, identityAtStart: identityAtStart, data: data, error: error)
        }
        if let requestCredentials {
            requestCredentials(completion)
            return
        }
        session?.sendMessage(["request": "watchCredentials"], replyHandler: { reply in
            let data = reply["credentials"] as? Data
            let error = reply["error"] as? String
            Task { @MainActor in
                completion(data, error)
            }
        }, errorHandler: { _ in
            Task { @MainActor in
                completion(nil, "Could not reach your iPhone. Open Family Assistant there and try again.")
            }
        })
    }

    private func receiveReply(id: UUID, identityAtStart: String?, data: Data?, error: String?) {
        guard requestID == id else { return }
        requestID = nil
        isConnecting = false
        guard let data else {
            message = error ?? "Could not connect. Try again with your iPhone nearby."
            return
        }
        do {
            let credentials = try JSONDecoder().decode(WatchCredentials.self, from: data)
            // A context cached before this request can predate the phone's login.
            // Only an update received during setup can supersede the live reply.
            if let identity = latestPhoneIdentity, identity != identityAtStart, identity != credentials.phoneSessionID {
                message = "iPhone sign-in changed. Please try again."
                return
            }
            try auth.installWatchCredentials(credentials)
            UserDefaults.standard.set(credentials.phoneSessionID, forKey: pairedSessionKey)
        } catch {
            message = "Could not save watch sign-in. Please try again."
        }
    }

    func receivePhoneState(_ identity: String) {
        latestPhoneIdentity = identity
        guard let previous = UserDefaults.standard.string(forKey: pairedSessionKey), previous != identity else { return }
        requestID = nil
        isConnecting = false
        auth.markAuthRequired()
        UserDefaults.standard.removeObject(forKey: pairedSessionKey)
        message = "Sign in on your iPhone, then set up your watch again."
    }

    nonisolated func session(_ session: WCSession, activationDidCompleteWith _: WCSessionActivationState, error _: Error?) {
        let identity = session.receivedApplicationContext["phoneSessionID"] as? String
        Task { @MainActor in
            if let identity { self.receivePhoneState(identity) }
            self.publishPhoneState()
        }
    }

    nonisolated func session(_: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        guard let identity = applicationContext["phoneSessionID"] as? String else { return }
        Task { @MainActor in self.receivePhoneState(identity) }
    }

    #if os(iOS)
    nonisolated func session(_: WCSession, didReceiveMessage message: [String: Any], replyHandler: @escaping ([String: Any]) -> Void) {
        guard message["request"] as? String == "watchCredentials" else {
            replyHandler(["error": "Unsupported watch request."])
            return
        }
        Task { @MainActor in
            do {
                let credentials = try await self.auth.provisionWatchCredentials()
                try replyHandler(["credentials": JSONEncoder().encode(credentials)])
            } catch {
                replyHandler(["error": "Open Family Assistant on your iPhone and sign in. If already signed in, check its connection and update the server."])
            }
        }
    }

    nonisolated func sessionDidBecomeInactive(_: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    nonisolated func sessionWatchStateDidChange(_: WCSession) {
        Task { @MainActor in self.publishPhoneState() }
    }
    #endif
}
