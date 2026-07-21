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
        guard satisfied != isSatisfied else { return }
        isSatisfied = satisfied
        onChange?(satisfied)
    }
}
