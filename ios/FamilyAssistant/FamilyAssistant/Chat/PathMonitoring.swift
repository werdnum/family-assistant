import Foundation
import Network

@MainActor
protocol PathMonitoring: AnyObject {
    var isSatisfied: Bool { get }
    var onChange: ((Bool) -> Void)? { get set }
    func start()
    func cancel()
}

@MainActor
final class NetworkPathMonitor: PathMonitoring {
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "com.familyassistant.pathmonitor")
    private var started = false

    private(set) var isSatisfied = false
    private var hasDelivered = false
    var onChange: ((Bool) -> Void)?

    func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            Task { @MainActor in
                self?.update(satisfied: satisfied)
            }
        }
        monitor.start(queue: queue)
    }

    func cancel() {
        guard started else { return }
        started = false
        monitor.cancel()
    }

    private func update(satisfied: Bool) {
        // Always deliver the first observation even when its value equals the
        // initial `isSatisfied` (false): launching offline reports `.unsatisfied`
        // first, which must reach the coordinator so it can leave `.unknown`.
        guard !hasDelivered || satisfied != isSatisfied else { return }
        hasDelivered = true
        isSatisfied = satisfied
        onChange?(satisfied)
    }
}
