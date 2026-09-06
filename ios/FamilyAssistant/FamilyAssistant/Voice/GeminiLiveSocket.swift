import Foundation

/// Minimal WebSocket surface the ``GeminiLiveClient`` depends on. Abstracted so
/// tests can drive the client with a fake socket instead of a real network
/// connection.
protocol GeminiLiveSocket: AnyObject {
    /// Send one JSON text frame.
    func send(_ text: String) async throws
    /// Await the next inbound frame, or nil for a clean end of stream.
    /// Transport and protocol failures throw.
    func receive() async throws -> Data?
    /// Close the connection.
    func close()
}

/// `URLSessionWebSocketTask`-backed implementation used in production.
final class URLSessionGeminiLiveSocket: GeminiLiveSocket {
    private let task: URLSessionWebSocketTask

    init(url: URL, urlSession: URLSession = .shared) {
        task = urlSession.webSocketTask(with: url)
        task.resume()
    }

    func send(_ text: String) async throws {
        try await task.send(.string(text))
    }

    func receive() async throws -> Data? {
        switch try await task.receive() {
        case .data(let data):
            return data
        case .string(let string):
            return Data(string.utf8)
        @unknown default:
            return Data()
        }
    }

    func close() {
        task.cancel(with: .normalClosure, reason: nil)
    }
}
