import Foundation

@testable import FamilyAssistant

@MainActor
final class StubPathMonitor: PathMonitoring {
    private(set) var isSatisfied: Bool
    private var hasDelivered = false
    var onChange: ((Bool) -> Void)?
    private(set) var startCount = 0
    private(set) var cancelCount = 0

    init(isSatisfied: Bool = true) {
        self.isSatisfied = isSatisfied
    }

    func start() {
        startCount += 1
    }

    func cancel() {
        cancelCount += 1
    }

    /// Mirror the production `NetworkPathMonitor` contract: the first observation
    /// is always delivered, even when its value equals the initial `isSatisfied`
    /// (launching offline reports `.unsatisfied` first). Subsequent same-value
    /// observations are still coalesced.
    func setSatisfied(_ satisfied: Bool) {
        guard !hasDelivered || satisfied != isSatisfied else { return }
        hasDelivered = true
        isSatisfied = satisfied
        onChange?(satisfied)
    }
}
