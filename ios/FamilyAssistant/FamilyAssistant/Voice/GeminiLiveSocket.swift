import Foundation

/// Only structural connection metadata crosses the diagnostics boundary. Never
/// serialize NSError.userInfo, setup frames, URLs, or arbitrary server text.
final class VoiceConnectionDiagnostics: @unchecked Sendable {
    typealias Sink = @Sendable (String, [String: String], Bool) -> Void
    private let id = UUID().uuidString
    private let started = Date()
    private let sink: Sink

    init(sink: @escaping Sink = { event, fields, failure in
        ErrorReporter.shared.report(
            message: "Voice connection \(event)", component: "Voice.connection",
            errorType: failure ? .handled : .component,
            extraData: fields, bypassDedupe: true
        )
    }) {
        self.sink = sink
    }

    func record(_ event: String, fields: [String: String] = [:], error: Error? = nil) {
        var fields = fields
        fields["attempt_id"] = id
        fields["event"] = event
        fields["occurred_at"] = ISO8601DateFormatter().string(from: Date())
        fields["elapsed_ms"] = String(Int(Date().timeIntervalSince(started) * 1000))
        if let error {
            var current: NSError? = error as NSError
            for depth in 0 ..< 3 {
                guard let value = current else { break }
                let prefix = depth == 0 ? "error" : "underlying_\(depth)"
                let domains = [NSURLErrorDomain, NSPOSIXErrorDomain, NSOSStatusErrorDomain, NSCocoaErrorDomain]
                fields["\(prefix)_domain"] = domains.contains(value.domain) ? value.domain : "other"
                fields["\(prefix)_code"] = String(value.code)
                current = value.userInfo[NSUnderlyingErrorKey] as? NSError
            }
        }
        sink(event, fields, error != nil)
    }

    static func closeFields(code: Int, reason: Data?) -> [String: String] {
        let text = reason.flatMap { String(data: $0, encoding: .utf8) }?.lowercased() ?? ""
        let category: String
        if text.contains("function") && (text.contains("limit") || text.contains("maximum") || text.contains("too many")) {
            category = "function_limit"
        } else if text.contains("schema") || text.contains("invalid argument") || text.contains("invalid json") {
            category = "invalid_setup"
        } else if text.contains("expired") {
            category = "expired"
        } else if text.contains("permission") || text.contains("unauthenticated") {
            category = "authentication"
        } else if text.contains("quota") || text.contains("resource exhausted") {
            category = "quota"
        } else {
            category = text.isEmpty ? "absent" : "unclassified"
        }
        return ["close_code": String(code), "close_reason_category": category,
                "close_reason_bytes": String(reason?.count ?? 0)]
    }
}

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
final class URLSessionGeminiLiveSocket: NSObject, GeminiLiveSocket, URLSessionWebSocketDelegate, @unchecked Sendable {
    private let task: URLSessionWebSocketTask
    private let diagnostics: VoiceConnectionDiagnostics?
    private let lock = NSLock()
    private var reportedFailure = false
    private var isClosing = false

    init(url: URL, urlSession: URLSession = .shared, diagnostics: VoiceConnectionDiagnostics? = nil) {
        self.diagnostics = diagnostics
        task = urlSession.webSocketTask(with: url)
        super.init()
        task.delegate = self
        task.resume()
    }

    func send(_ text: String) async throws {
        do {
            try await task.send(.string(text))
        } catch {
            recordFailure(error)
            throw error
        }
    }

    func receive() async throws -> Data? {
        let message: URLSessionWebSocketTask.Message
        do {
            message = try await task.receive()
        } catch {
            recordFailure(error)
            throw error
        }
        switch message {
        case let .data(data):
            return data
        case let .string(string):
            return Data(string.utf8)
        @unknown default:
            return Data()
        }
    }

    private func recordFailure(_ error: Error) {
        let shouldReport = lock.withLock {
            if reportedFailure || isClosing { return false }
            reportedFailure = true
            return true
        }
        guard shouldReport else { return }
        var fields = VoiceConnectionDiagnostics.closeFields(code: task.closeCode.rawValue, reason: task.closeReason)
        if let response = task.response as? HTTPURLResponse {
            fields["http_status"] = String(response.statusCode)
        }
        diagnostics?.record("socket_failure", fields: fields, error: error)
    }

    func urlSession(_: URLSession, webSocketTask _: URLSessionWebSocketTask, didOpenWithProtocol _: String?) {
        diagnostics?.record("socket_open")
    }

    func urlSession(_: URLSession, webSocketTask _: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?)
    {
        diagnostics?.record("socket_closed", fields: VoiceConnectionDiagnostics.closeFields(code: closeCode.rawValue, reason: reason))
    }

    func urlSession(_: URLSession, task _: URLSessionTask, didCompleteWithError error: Error?) {
        if let error { recordFailure(error) }
    }

    func close() {
        lock.withLock { isClosing = true }
        task.cancel(with: .normalClosure, reason: nil)
    }
}
