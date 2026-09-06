import Foundation

/// Errors surfaced by the Gemini Live client.
enum GeminiLiveError: LocalizedError, Equatable {
    case notConnected
    case invalidEndpoint

    var errorDescription: String? {
        switch self {
        case .notConnected:
            "The voice session is not connected."
        case .invalidEndpoint:
            "Could not build the voice service URL."
        }
    }
}

/// Drives a direct WebSocket session with the Gemini Live API.
///
/// Connection is direct-to-Google using the backend-minted ephemeral token; the
/// app never proxies audio through its own server. The client is intentionally
/// thin: it owns the socket, frames the protocol via ``GeminiLiveCodec``, and
/// republishes decoded server messages on ``events``. Audio capture/playback and
/// session policy live in higher layers.
@MainActor
final class GeminiLiveClient {
    typealias SocketFactory = @Sendable (URL) -> GeminiLiveSocket

    nonisolated static let defaultHost = "generativelanguage.googleapis.com"
    nonisolated static let apiVersion = "v1alpha"

    /// Ordered stream of decoded server events. Finishes when the connection ends
    /// (via ``close()`` or a socket failure); inspect ``lastError`` afterward to
    /// tell a clean close from a failure.
    let events: AsyncStream<GeminiLiveServerEvent>
    private(set) var lastError: Error?

    private let continuation: AsyncStream<GeminiLiveServerEvent>.Continuation
    private let host: String
    private let socketFactory: SocketFactory
    private var socket: GeminiLiveSocket?
    private var receiveTask: Task<Void, Never>?
    private var isClosed = false
    private let diagnostics: VoiceConnectionDiagnostics?

    init(
        host: String = GeminiLiveClient.defaultHost,
        diagnostics: VoiceConnectionDiagnostics? = nil,
        socketFactory: SocketFactory? = nil
    ) {
        self.diagnostics = diagnostics
        self.host = host
        self.socketFactory = socketFactory ?? { url in
            #if os(watchOS)
                NetworkGeminiLiveSocket(url: url, diagnostics: diagnostics)
            #else
                URLSessionGeminiLiveSocket(url: url, diagnostics: diagnostics)
            #endif
        }
        (events, continuation) = AsyncStream.makeStream(of: GeminiLiveServerEvent.self)
    }

    /// Open the socket and send the setup frame. Begins receiving on success.
    func connect(token: EphemeralToken) async throws {
        guard socket == nil, !isClosed else { return }
        let url = try Self.endpointURL(token: token.token, host: host)
        let setup = try GeminiLiveCodec.setupMessage(for: token)
        diagnostics?.record("socket_start", fields: ["api_version": Self.apiVersion, "setup_bytes": String(setup.utf8.count)])
        let socket = socketFactory(url)
        self.socket = socket
        do {
            try await socket.send(setup)
            diagnostics?.record("setup_sent")
        } catch {
            socket.close()
            throw error
        }
        guard !isClosed else {
            socket.close()
            return
        }
        receiveTask = Task { [weak self] in
            await self?.runReceiveLoop(socket: socket)
        }
    }

    /// Stream a chunk of microphone audio (16 kHz mono PCM16).
    func sendAudio(_ pcm16: Data) async throws {
        guard let socket else { throw GeminiLiveError.notConnected }
        try await socket.send(GeminiLiveCodec.audioMessage(base64PCM16: pcm16.base64EncodedString()))
    }

    /// Tell the server the client paused sending audio.
    func endAudioStream() async throws {
        guard let socket else { return }
        try await socket.send(GeminiLiveCodec.audioStreamEndMessage())
    }

    /// Return tool results so the model can continue the turn.
    func sendToolResponses(_ responses: [GeminiFunctionResponse]) async throws {
        guard let socket else { throw GeminiLiveError.notConnected }
        try await socket.send(GeminiLiveCodec.toolResponseMessage(responses))
    }

    /// Tear down the session. Idempotent.
    func close() {
        guard !isClosed else { return }
        isClosed = true
        receiveTask?.cancel()
        receiveTask = nil
        socket?.close()
        socket = nil
        continuation.finish()
    }

    private func runReceiveLoop(socket: GeminiLiveSocket) async {
        defer { socket.close() }
        do {
            while !Task.isCancelled {
                guard let data = try await socket.receive() else { break }
                // A frame that fails to decode is a protocol violation, not noise:
                // let it throw so the session ends with lastError set rather than
                // hanging while the UI waits for events that will never arrive.
                for event in try GeminiLiveCodec.decodeServerMessage(data) {
                    continuation.yield(event)
                }
            }
        } catch is CancellationError {
            // Expected when close() cancels the loop.
        } catch {
            if !isClosed {
                lastError = error
            }
        }
        // The socket ended on its own (server close / network drop); finish the
        // stream so the consumer observes the disconnect. A caller-initiated
        // close() already finished it and set isClosed.
        if !isClosed {
            isClosed = true
            self.socket = nil
            continuation.finish()
        }
    }

    /// Build the Live API endpoint for a token.
    ///
    /// Ephemeral tokens (their name begins with `auth_tokens/`) authenticate via
    /// the `access_token` query parameter against the *Constrained* RPC; a raw API
    /// key would use `key=` against plain `BidiGenerateContent`. The backend always
    /// mints ephemeral tokens.
    static func endpointURL(token: String, host: String) throws -> URL {
        let isEphemeral = token.hasPrefix("auth_tokens/")
        let method = isEphemeral ? "BidiGenerateContentConstrained" : "BidiGenerateContent"
        let queryName = isEphemeral ? "access_token" : "key"
        var components = URLComponents()
        components.scheme = "wss"
        components.host = host
        components.path = "/ws/google.ai.generativelanguage.\(apiVersion).GenerativeService.\(method)"
        components.queryItems = [URLQueryItem(name: queryName, value: token)]
        guard let url = components.url else {
            throw GeminiLiveError.invalidEndpoint
        }
        return url
    }
}
