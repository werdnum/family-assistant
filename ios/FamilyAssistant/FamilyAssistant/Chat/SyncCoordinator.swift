import Foundation

@MainActor
@Observable
final class SyncCoordinator {
    enum Lifecycle {
        case foreground
        case background
    }

    enum Reachability {
        case satisfied
        case unsatisfied
        case unknown
    }

    enum AuthState {
        case ok
        case refreshing
        case authRequired
    }

    enum ChannelHealth {
        case connected
        case reconnecting
        case down
    }

    enum ReconciliationPhase {
        case idle
        case syncing
    }

    enum Presentation {
        case live
        case syncing
        case degraded
        case offline
        case authRequired
        case suspended
    }

    enum SyncEvent {
        case foregrounded
        case backgrounded
        case reachabilityChanged(Reachability)
        case authRefreshing
        case authOK
        case authRequired
        case followConnected(generation: Int)
        case followDropped(generation: Int, cleanEOF: Bool)
        case activityConnected(generation: Int)
        case activityDropped(generation: Int, cleanEOF: Bool)
        case syncStarted
        case syncFinished
    }

    enum SyncEffect: Equatable {
        case startFollowStream(generation: Int)
        case startActivityStream(generation: Int)
        case cancelStreams
        case runResync(generation: Int)
    }

    private(set) var lifecycle: Lifecycle = .foreground
    private(set) var reachability: Reachability = .unknown
    private(set) var authState: AuthState = .ok
    private(set) var followHealth: ChannelHealth = .down
    private(set) var activityHealth: ChannelHealth = .down
    private(set) var phase: ReconciliationPhase = .idle
    private(set) var cameFromBackground = false
    private(set) var generation = 0

    private let pathMonitor: PathMonitoring

    init(pathMonitor: PathMonitoring) {
        self.pathMonitor = pathMonitor
        pathMonitor.onChange = { [weak self] satisfied in
            self?.apply(.reachabilityChanged(satisfied ? .satisfied : .unsatisfied))
        }
        pathMonitor.start()
        if pathMonitor.isSatisfied {
            reachability = .satisfied
        }
    }

    var presentation: Presentation {
        if lifecycle == .background {
            return .suspended
        }
        if authState == .authRequired {
            return .authRequired
        }
        if reachability == .unsatisfied {
            return .offline
        }
        if phase == .syncing {
            return .syncing
        }
        if followHealth == .connected, activityHealth == .connected {
            return .live
        }
        return .degraded
    }

    func isCurrent(_ generation: Int) -> Bool {
        generation == self.generation
    }

    @discardableResult
    func bumpGeneration() -> Int {
        generation += 1
        return generation
    }

    /// Maps a raw scene-phase observation onto the coordinator's lifecycle events. The
    /// `didBackground`/`isActive` split lets callers distinguish a real background (which must latch
    /// `cameFromBackground`) from an `.inactive` blip that never backgrounded.
    func scenePhaseChanged(didBackground: Bool, isActive: Bool) {
        if didBackground {
            apply(.backgrounded)
        } else if isActive {
            apply(.foregrounded)
        }
    }

    @discardableResult
    func apply(_ event: SyncEvent) -> [SyncEffect] {
        switch event {
        case .foregrounded:
            lifecycle = .foreground
            guard cameFromBackground else {
                return []
            }
            cameFromBackground = false
            let newGeneration = bumpGeneration()
            return [.runResync(generation: newGeneration)]

        case .backgrounded:
            lifecycle = .background
            cameFromBackground = true
            return []

        case let .reachabilityChanged(reachability):
            self.reachability = reachability
            return []

        case .authRefreshing:
            authState = .refreshing
            return []

        case .authOK:
            authState = .ok
            return []

        case .authRequired:
            authState = .authRequired
            return []

        case let .followConnected(generation):
            guard isCurrent(generation) else {
                return []
            }
            followHealth = .connected
            return []

        case let .followDropped(generation, _):
            guard isCurrent(generation) else {
                return []
            }
            followHealth = .down
            return []

        case let .activityConnected(generation):
            guard isCurrent(generation) else {
                return []
            }
            activityHealth = .connected
            return []

        case let .activityDropped(generation, _):
            guard isCurrent(generation) else {
                return []
            }
            activityHealth = .down
            return []

        case .syncStarted:
            phase = .syncing
            return []

        case .syncFinished:
            phase = .idle
            return []
        }
    }
}
