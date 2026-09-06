import Foundation
import Network

/// watchOS performs streaming audio networking in-process after audio activation.
final class NetworkGeminiLiveSocket: GeminiLiveSocket {
    private let connection: NWConnection

    init(url: URL) {
        let parameters = NWParameters.tls
        let webSocket = NWProtocolWebSocket.Options()
        webSocket.autoReplyPing = true
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        (parameters.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options)?.connectionTimeout = 20
        connection = NWConnection(to: .url(url), using: parameters)
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

    func receive() async throws -> Data? {
        while true {
            let (data, metadata) = try await receiveFrame()
            switch metadata.opcode {
            case .text, .binary:
                return data
            case .ping, .pong:
                continue
            case .close:
                guard metadata.closeCode == .protocolCode(.normalClosure)
                    || metadata.closeCode == .protocolCode(.goingAway) else {
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
