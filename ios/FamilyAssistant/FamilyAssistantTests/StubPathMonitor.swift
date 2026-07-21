import Foundation

@testable import FamilyAssistant

@MainActor
final class StubPathMonitor: PathMonitoring {
    private(set) var isSatisfied: Bool
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

    func setSatisfied(_ satisfied: Bool) {
        guard satisfied != isSatisfied else { return }
        isSatisfied = satisfied
        onChange?(satisfied)
    }
}
