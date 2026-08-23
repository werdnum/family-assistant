import Foundation
import os

/// Reports native iOS errors to the backend `POST /api/errors/` endpoint so problems surfaced in a
/// TestFlight build land in the same server-side error log as web-frontend errors, without relying
/// on the tester to describe them.
///
/// Delivery is best-effort. When the server URL is not yet known (e.g. the error happened before
/// sign-in) or the network send fails, the report is spooled to disk and retried by
/// ``flushPersisted()`` on the next launch. Uncaught Objective-C exceptions are persisted
/// synchronously from the `NSSetUncaughtExceptionHandler` callback, since the process is
/// terminating and an asynchronous send would not complete.
///
/// Hard crashes (Swift traps, signals) are intentionally out of scope: Apple already captures those
/// for TestFlight builds. See `docs/design/ios_error_reporting.md`.
///
/// Each report carries a severity (derived from its ``ErrorType``). Real errors (`.handled`,
/// `.uncaught`) land in the backend error log; diagnostic breadcrumbs (`.component`) are routed to
/// a separate telemetry lane so they do not drown genuine errors in the engineer's error view. See
/// `docs/design/ios-frontend-telemetry-lane.md`.
final class ErrorReporter: @unchecked Sendable {
    static let shared = ErrorReporter()

    enum ErrorType: String {
        case uncaught
        case handled = "manual"
        case component = "component_error"

        /// Severity lane the backend routes this report to. `.component` is used
        /// exclusively for diagnostic breadcrumbs (transport events, resync phases,
        /// alert/inline-error counters, the sign-in watchdog note), so it goes to the
        /// telemetry ring buffer ("info") rather than the error log. Real caught
        /// errors (`.handled`) and uncaught exceptions stay in the error lane.
        var severity: String {
            switch self {
            case .component:
                return "info"
            case .handled, .uncaught:
                return "error"
            }
        }
    }

    private let session: URLSession
    private let spoolDirectory: URL?
    private let dedupeWindow: TimeInterval
    private let logger = Logger(subsystem: "com.familyassistant.app", category: "error-reporting")

    private let lock = NSLock()
    private var baseURLProvider: (() -> URL?)?
    private var authTokenProvider: (() async throws -> String?)?
    private var recentReports: [String: Date] = [:]

    /// Cap on spooled reports so a server outage cannot grow the cache without bound.
    private let maxSpooledReports = 50

    private static var previousExceptionHandler: (@convention(c) (NSException) -> Void)?

    init(
        session: URLSession = .shared,
        spoolDirectory: URL? = ErrorReporter.defaultSpoolDirectory,
        dedupeWindow: TimeInterval = 60
    ) {
        self.session = session
        self.spoolDirectory = spoolDirectory
        self.dedupeWindow = dedupeWindow
    }

    // MARK: - Configuration

    /// Provide a resolver for the backend base URL (e.g. `https://assistant.example.com`).
    /// The token provider supplies the current API access token so reports keep
    /// landing in the persistent error lane on authenticated deployments.
    func configure(
        baseURLProvider: @escaping () -> URL?,
        authTokenProvider: (() async throws -> String?)? = nil
    ) {
        lock.withLock {
            self.baseURLProvider = baseURLProvider
            self.authTokenProvider = authTokenProvider
        }
    }

    /// Install a global uncaught-exception handler that persists the exception for delivery on the
    /// next launch. Chains to any previously-installed handler.
    func installGlobalHandlers() {
        Self.previousExceptionHandler = NSGetUncaughtExceptionHandler()
        NSSetUncaughtExceptionHandler { exception in
            ErrorReporter.shared.persistException(exception)
            ErrorReporter.previousExceptionHandler?(exception)
        }
    }

    // MARK: - Reporting

    /// Report a Swift `Error` surfaced to the user. Fire-and-forget.
    func report(_ error: Error, component: String, errorType: ErrorType = .handled) {
        let message = error.localizedDescription
        let typeName = String(describing: type(of: error))
        Task { [weak self] in
            await self?.deliver(
                message: message,
                component: component,
                errorType: errorType,
                stack: nil,
                extraData: ["error_type_name": typeName]
            )
        }
    }

    /// Report a free-form message. Fire-and-forget. `bypassDedupe` is for events that count
    /// discrete occurrences (e.g. alert presentations), where dropping repeats inside the dedupe
    /// window would undercount.
    func report(
        message: String,
        component: String,
        errorType: ErrorType = .handled,
        stack: String? = nil,
        extraData: [String: String] = [:],
        bypassDedupe: Bool = false
    ) {
        Task { [weak self] in
            await self?.deliver(
                message: message,
                component: component,
                errorType: errorType,
                stack: stack,
                extraData: extraData,
                bypassDedupe: bypassDedupe
            )
        }
    }

    /// Awaitable delivery used by ``report(...)`` and tests. Deduplicates, then sends (spooling on
    /// failure).
    func deliver(
        message: String,
        component: String,
        errorType: ErrorType,
        stack: String?,
        extraData: [String: String],
        bypassDedupe: Bool = false
    ) async {
        guard bypassDedupe || !shouldDedupe(message: message, component: component, errorType: errorType)
        else {
            return
        }
        let payload = makePayload(
            message: message,
            component: component,
            errorType: errorType,
            stack: stack,
            extraData: extraData
        )
        await send(payload)
    }

    /// Retry any reports spooled to disk by a previous launch or a failed send. Stops on the first
    /// failure so a persistent outage does not hammer the server.
    func flushPersisted() async {
        guard let directory = spoolDirectory,
              let files = try? FileManager.default.contentsOfDirectory(
                  at: directory,
                  includingPropertiesForKeys: nil
              ),
              !files.isEmpty,
              let baseURL = currentBaseURL()
        else {
            return
        }

        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let payload = try? JSONDecoder().decode(ErrorReportPayload.self, from: data)
            else {
                try? FileManager.default.removeItem(at: file)
                continue
            }
            do {
                try await post(payload, to: baseURL)
                try? FileManager.default.removeItem(at: file)
            } catch {
                break
            }
        }
    }

    // MARK: - Delivery

    private func send(_ payload: ErrorReportPayload) async {
        guard let baseURL = currentBaseURL() else {
            persist(payload)
            return
        }
        do {
            try await post(payload, to: baseURL)
        } catch {
            persist(payload)
        }
    }

    private func post(_ payload: ErrorReportPayload, to baseURL: URL) async throws {
        guard let url = URL(string: "/api/errors/", relativeTo: baseURL)?.absoluteURL else {
            throw ReporterError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let tokenProvider = lock.withLock { authTokenProvider }
        let token: String?
        do {
            token = try await tokenProvider?()
        } catch {
            // Authentication enriches reports but must not block the public error-intake path.
            token = nil
        }
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = try JSONEncoder().encode(payload)

        let (data, response) = try await session.dataExpectingJSON(for: request, authWallError: ReporterError.authWall)
        try AuthWallDetection.rejectIfLikely(
            response: response,
            data: data,
            throwing: ReporterError.authWall
        )
        guard let httpResponse = response as? HTTPURLResponse,
              200..<300 ~= httpResponse.statusCode
        else {
            throw ReporterError.badResponse
        }
    }

    private func currentBaseURL() -> URL? {
        let provider = lock.withLock { baseURLProvider }
        return provider?()
    }

    // MARK: - Deduplication

    private func shouldDedupe(message: String, component: String, errorType: ErrorType) -> Bool {
        let key = "\(message)|\(component)|\(errorType.rawValue)"
        return lock.withLock {
            let now = Date()
            recentReports = recentReports.filter { now.timeIntervalSince($0.value) < dedupeWindow }
            if let last = recentReports[key], now.timeIntervalSince(last) < dedupeWindow {
                return true
            }
            recentReports[key] = now
            return false
        }
    }

    // MARK: - Persistence

    private func persistException(_ exception: NSException) {
        let payload = makePayload(
            message: "\(exception.name.rawValue): \(exception.reason ?? "")",
            component: "UncaughtException",
            errorType: .uncaught,
            stack: exception.callStackSymbols.joined(separator: "\n"),
            extraData: [:]
        )
        persist(payload)
    }

    private func persist(_ payload: ErrorReportPayload) {
        guard let directory = spoolDirectory else { return }
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            let fileURL = directory.appendingPathComponent("\(UUID().uuidString).json")
            try JSONEncoder().encode(payload).write(to: fileURL, options: .atomic)
            trimSpool(in: directory)
        } catch {
            logger.error(
                "Failed to persist error report: \(error.localizedDescription, privacy: .public)"
            )
        }
    }

    /// Keep the spool directory bounded by deleting the oldest reports beyond ``maxSpooledReports``.
    private func trimSpool(in directory: URL) {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.creationDateKey]
        ) else {
            return
        }
        let jsonFiles = files.filter { $0.pathExtension == "json" }
        guard jsonFiles.count > maxSpooledReports else { return }

        let sorted = jsonFiles.sorted { lhs, rhs in
            let lhsDate = (try? lhs.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
            let rhsDate = (try? rhs.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
            return lhsDate < rhsDate
        }
        for file in sorted.prefix(jsonFiles.count - maxSpooledReports) {
            try? FileManager.default.removeItem(at: file)
        }
    }

    // MARK: - Payload construction

    private func makePayload(
        message: String,
        component: String,
        errorType: ErrorType,
        stack: String?,
        extraData: [String: String]
    ) -> ErrorReportPayload {
        ErrorReportPayload(
            message: message,
            stack: stack,
            url: Self.syntheticURL(component: component),
            userAgent: Self.userAgent,
            componentName: component,
            errorType: errorType.rawValue,
            severity: errorType.severity,
            extraData: Self.metadata().merging(extraData) { _, new in new }
        )
    }

    private static func syntheticURL(component: String) -> String {
        let encoded = component.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? component
        return "familyassistant://ios/\(encoded)"
    }

    private static var userAgent: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "unknown"
        let build = info?["CFBundleVersion"] as? String ?? "unknown"
        return "FamilyAssistant-iOS/\(version) (build \(build); "
            + "\(ProcessInfo.processInfo.operatingSystemVersionString))"
    }

    private static func metadata() -> [String: String] {
        let info = Bundle.main.infoDictionary
        var data: [String: String] = [
            "platform": "ios",
            "app_version": info?["CFBundleShortVersionString"] as? String ?? "unknown",
            "build": info?["CFBundleVersion"] as? String ?? "unknown",
            "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
            "is_testflight": isTestFlight ? "true" : "false",
        ]
        if let installationID = UserDefaults.standard.string(forKey: "fa_installation_id") {
            data["installation_id"] = installationID
        }
        return data
    }

    /// A TestFlight (sandbox) build ships a `sandboxReceipt` rather than a production receipt.
    static var isTestFlight: Bool {
        Bundle.main.appStoreReceiptURL?.lastPathComponent == "sandboxReceipt"
    }

    static var defaultSpoolDirectory: URL? {
        FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("PendingErrorReports", isDirectory: true)
    }

    private enum ReporterError: Error {
        case invalidURL
        case badResponse
        case authWall
    }
}

/// Wire payload matching the backend `FrontendErrorReport` model (`errors_api.py`).
struct ErrorReportPayload: Codable, Sendable {
    let message: String
    let stack: String?
    let url: String
    let userAgent: String?
    let componentName: String?
    let errorType: String?
    let severity: String?
    let extraData: [String: String]?

    enum CodingKeys: String, CodingKey {
        case message
        case stack
        case url
        case userAgent = "user_agent"
        case componentName = "component_name"
        case errorType = "error_type"
        case severity
        case extraData = "extra_data"
    }
}
