import Foundation
import Network

@MainActor
protocol PathMonitoring: AnyObject {
    var isSatisfied: Bool { get }
    var interfaceType: String { get }
    var onChange: ((Bool) -> Void)? { get set }
    func start()
    func cancel()
}

extension PathMonitoring {
    var interfaceType: String { "unknown" }
}

@MainActor
final class NetworkPathMonitor: PathMonitoring {
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "com.familyassistant.pathmonitor")
    private var started = false

    private(set) var isSatisfied = false
    private(set) var interfaceType = "unknown"
    private var hasDelivered = false
    var onChange: ((Bool) -> Void)?

    func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            let interfaceType = Self.interfaceType(for: path)
            Task { @MainActor in
                self?.update(satisfied: satisfied, interfaceType: interfaceType)
            }
        }
        monitor.start(queue: queue)
    }

    func cancel() {
        guard started else { return }
        started = false
        monitor.cancel()
    }

    private func update(satisfied: Bool, interfaceType: String) {
        self.interfaceType = interfaceType
        // Always deliver the first observation even when its value equals the
        // initial `isSatisfied` (false): launching offline reports `.unsatisfied`
        // first, which must reach the coordinator so it can leave `.unknown`.
        guard !hasDelivered || satisfied != isSatisfied else { return }
        hasDelivered = true
        isSatisfied = satisfied
        onChange?(satisfied)
    }

    nonisolated private static func interfaceType(for path: NWPath) -> String {
        if path.usesInterfaceType(.wifi) {
            return "wifi"
        }
        if path.usesInterfaceType(.cellular) {
            return "cellular"
        }
        return "other"
    }
}
