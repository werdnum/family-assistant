import Foundation
import Network

/// watchOS performs streaming audio networking in-process after audio activation.
final class NetworkGeminiLiveSocket: GeminiLiveSocket {
    private let connection: NWConnection
    private let diagnostics: VoiceConnectionDiagnostics?

    init(url: URL, diagnostics: VoiceConnectionDiagnostics? = nil) {
        self.diagnostics = diagnostics
        let parameters = NWParameters.tls
        let webSocket = NWProtocolWebSocket.Options()
        webSocket.autoReplyPing = true
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        (parameters.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options)?.connectionTimeout = 20
        connection = NWConnection(to: .url(url), using: parameters)
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                diagnostics?.record("socket_open")
            case let .waiting(error):
                diagnostics?.record("socket_waiting", fields: Self.errorFields(error))
            case let .failed(error):
                diagnostics?.record("socket_failure", fields: Self.errorFields(error), error: error)
            case .cancelled:
                diagnostics?.record("socket_cancelled")
            default:
                break
            }
        }
        connection.start(queue: DispatchQueue(label: "dev.andrewgarrett.assistant.watch.voice"))
    }

    func send(_ text: String) async throws {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "voice", metadata: [metadata])
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: Data(text.utf8), contentContext: context, isComplete: true,
                            completion: .contentProcessed { error in
                                if let error { continuation.resume(throwing: error) }
                                else { continuation.resume() }
                            })
        }
    }

    private static func errorFields(_ error: NWError) -> [String: String] {
        switch error {
        case let .posix(code): ["network_error_domain": "posix", "network_error_code": String(code.rawValue)]
        case let .dns(code): ["network_error_domain": "dns", "network_error_code": String(code)]
        case let .tls(code): ["network_error_domain": "tls", "network_error_code": String(code)]
        @unknown default: ["network_error_domain": "unknown"]
        }
    }

    func receive() async throws -> Data? {
        while true {
            let (data, metadata) = try await receiveFrame()
            switch metadata.opcode {
            case .text, .binary:
                return data
            case .ping, .pong:
                continue
            case .close:
                let code: UInt16
                switch metadata.closeCode {
                case let .protocolCode(value): code = value.rawValue
                case let .applicationCode(value): code = value
                case let .privateCode(value): code = value
                @unknown default: code = 0
                }
                diagnostics?.record("socket_closed", fields: VoiceConnectionDiagnostics.closeFields(code: Int(code), reason: data))
                guard metadata.closeCode == .protocolCode(.normalClosure)
                    || metadata.closeCode == .protocolCode(.goingAway)
                else {
                    throw GeminiLiveError.notConnected
                }
                return nil
            default:
                throw GeminiLiveError.notConnected
            }
        }
    }

    private func receiveFrame() async throws -> (Data, NWProtocolWebSocket.Metadata) {
        try await withCheckedThrowingContinuation { continuation in
            connection.receiveMessage { data, context, _, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                    as? NWProtocolWebSocket.Metadata
                {
                    continuation.resume(returning: (data ?? Data(), metadata))
                } else {
                    continuation.resume(throwing: GeminiLiveError.notConnected)
                }
            }
        }
    }

    func close() {
        connection.cancel()
    }
}
