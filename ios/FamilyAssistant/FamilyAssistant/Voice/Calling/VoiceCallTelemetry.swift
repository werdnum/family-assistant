import Foundation

/// Where the Siri call path leaves its breadcrumbs.
///
/// Everything between Siri and a running call happens on a device, in hooks a
/// simulator never exercises, and with no channel back to the user: a request
/// that is dropped anywhere along the way looks exactly like a request that was
/// never made. Each step therefore records that it was reached and what it
/// decided, so one device test tells the steps apart.
///
/// Injected rather than reached for, so the records a step makes are part of
/// what its tests assert.
protocol VoiceCallTelemetryRecording: Sendable {
    func record(_ event: String, component: String, extraData: [String: String])
}

extension VoiceCallTelemetryRecording {
    func record(_ event: String, component: String) {
        record(event, component: component, extraData: [:])
    }
}

/// The components the call path records under. Named in one place because they
/// are read by grepping the telemetry lane for `Voice.call.`, and a name that
/// drifts is a step that silently stops being findable.
enum VoiceCallTelemetryComponent {
    /// A user activity arrived at the app, whatever its type.
    static let activity = "Voice.call.activity"
    /// A start-call activity's destination was resolved.
    static let destination = "Voice.call.destination"
    /// A start-call request reached the request center.
    static let request = "Voice.call.request"
    /// The starter was asked for a call.
    static let starting = "Voice.call.starting"
    /// A call was placed: CallKit's start action was fulfilled and the session
    /// behind it was started. Recorded by the coordinator, because that is the
    /// only place the outcome is known — the transaction the starter awaits is
    /// accepted well before anything that can still fail the start has run.
    static let started = "Voice.call.started"
    /// CallKit accepted the transaction and the start action was failed anyway.
    static let startFailed = "Voice.call.startFailed"
    /// Whether the device was unlocked when the call was asked for, and — for
    /// a locked device — the moment the call's route let it through.
    static let admission = "Voice.call.admission"
    /// CallKit activated the call's audio session, and the route it chose.
    static let route = "Voice.call.route"
    /// A hands-free call was refused: the device was locked when it was asked
    /// for, and the call's audio never reached CarPlay.
    static let handsFreeRefused = "Voice.call.handsFreeAccess"
    /// A request arrived while a call was already running.
    static let duplicate = "Voice.call.duplicate"
    /// A request arrived with nothing to authenticate the voice socket with.
    static let signedOut = "Voice.call.signedOut"
}

/// The shipped sink: the `.component` lane, which the backend routes to its
/// telemetry ring buffer rather than the error log. These are breadcrumbs, not
/// errors — a refused call is a decision, and only a thrown error is reported
/// as one.
///
/// Dedupe is bypassed because the questions these answer are "how many" and "in
/// what order": collapsing a second identical arrival inside the dedupe window
/// would hide the repeat that distinguishes a retried request from a lost one.
struct VoiceCallTelemetry: VoiceCallTelemetryRecording {
    static let shared = VoiceCallTelemetry()

    private let reporter: ErrorReporter

    init(reporter: ErrorReporter = .shared) {
        self.reporter = reporter
    }

    func record(_ event: String, component: String, extraData: [String: String]) {
        reporter.report(
            message: event,
            component: component,
            errorType: .component,
            extraData: extraData,
            bypassDedupe: true
        )
    }
}
