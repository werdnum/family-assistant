import SwiftUI
import UIKit
import XCTest

@testable import FamilyAssistant

/// Layout-budget harness for the chat thread. The production scene-update
/// watchdog crash (0x8BADF00D, scratch/FamilyAssistant-2026-06-27-192549.ips) is
/// one instance of a class of bug: a single chat message whose view tree is so
/// expensive to lay out that the main-thread content-sizing pass overruns the
/// watchdog. Rather than testing one shape, this enforces the invariant directly
/// — *any* single message must lay out within a wall-clock budget well under the
/// 10s watchdog — across an explicit adversarial matrix and a seeded fuzzer, so
/// related issues surface as a group. See
/// docs/design/ios-chat-layout-watchdog-crash.md.
///
/// Timing is host-speed dependent, so the budget is deliberately generous (an
/// order of magnitude above a bounded message, an order of magnitude below the
/// watchdog): a bounded layout is single-digit/low-hundreds of milliseconds; a
/// pathological one is multiple seconds.
@MainActor
final class ChatLayoutBudgetTests: XCTestCase {
    /// Per-message layout budget. The scene-update watchdog fires at 10s; a
    /// healthy bounded message lays out in well under 100ms on the test host.
    private static let budgetSeconds: Double = 1.5

    /// iPhone-class content width/height the bubble is sized against.
    private static let width: CGFloat = 393
    private static let height: CGFloat = 852

    // MARK: - Adversarial matrix

    /// Named content shapes, each at a size large enough that an *unbounded*
    /// renderer blows the budget while a bounded one stays flat. A bounded chat
    /// renderer must keep all of these under budget.
    func testSingleMessageLayoutStaysBoundedAcrossShapes() {
        let shapes = Self.adversarialShapes()

        var report: [String] = []
        var offenders: [String] = []
        for shape in shapes {
            let seconds = layoutSeconds(for: [assistantMessage(shape.markdown, id: shape.name)])
            let line = String(format: "  %-26@  %7.3fs%@", shape.name as NSString, seconds,
                              seconds > Self.budgetSeconds ? "  ❌ OVER BUDGET" : "")
            report.append(line)
            if seconds > Self.budgetSeconds {
                offenders.append("\(shape.name) (\(String(format: "%.3f", seconds))s)")
            }
        }

        attach(report: report.joined(separator: "\n"), named: "layout-budget-matrix")
        XCTAssertTrue(
            offenders.isEmpty,
            "Message shapes exceeded the \(Self.budgetSeconds)s layout budget (watchdog-hang risk):\n" +
                offenders.joined(separator: "\n") + "\n\nFull timings:\n" + report.joined(separator: "\n")
        )
    }

    /// Large adversarial content shapes. Sizes are large enough that an
    /// *unbounded* renderer blows the budget (by seconds) yet still terminates —
    /// so a future regression that removes a cap fails loudly instead of wedging
    /// the suite (a synchronous main-thread layout cannot be interrupted). A
    /// correctly bounded renderer realizes a constant ~`leafBudget` views
    /// regardless of these sizes.
    static func adversarialShapes() -> [(name: String, markdown: String)] {
        [
            ("plain-baseline", MarkdownShapes.paragraphs(3)),
            ("many-paragraphs", MarkdownShapes.paragraphs(2000)),
            ("many-headings", MarkdownShapes.headings(2000)),
            ("huge-single-list", MarkdownShapes.bigList(1500)),
            ("deeply-nested-list", MarkdownShapes.nestedList(depth: 40)),
            ("tall-table", MarkdownShapes.bigTable(rows: 600, cols: 5)),
            ("wide-table", MarkdownShapes.bigTable(rows: 30, cols: 120)),
            ("giant-code-block", MarkdownShapes.giantCodeBlock(lines: 2500)),
            ("one-huge-unbroken-word", MarkdownShapes.longUnbrokenWord(40_000)),
            ("inline-formatting-storm", MarkdownShapes.longInlineFormatting(2500)),
            ("everything-mixed", MarkdownShapes.everything(repeats: 250)),
            // Plain prose with no markdown markers exercises the plain branch of
            // renderPlan, which must also stay bounded (one giant Text is a
            // watchdog hazard too).
            ("huge-plain-prose", String(repeating: "word ", count: 60_000)),
        ]
    }

    // MARK: - Fast structural guard (no view hosting; cannot wedge)

    /// Validates the *bound itself* — purely, in milliseconds, with no layout —
    /// for every adversarial and fuzzed shape: the render plan the view will
    /// actually display has a small leaf count and no oversized `Text`. A
    /// regression that unbounds a shape (e.g. wide tables) is caught here
    /// instantly, instead of wedging the timing test for tens of minutes.
    func testRenderPlanStaysBoundedForEveryShape() {
        let maxLeaves = MarkdownRenderBudget.leafBudget * 4
        let maxText = MarkdownRenderBudget.textCharCap + 1 // capped text appends one ellipsis
        let maxDepth = MarkdownRenderBudget.maxNestingDepth + 2 // + the collapse marker
        var offenders: [String] = []

        let maxPlainChars = MarkdownRenderBudget.charsPerPage
        func check(_ label: String, _ markdown: String) {
            switch MarkdownRenderBudget.renderPlan(for: markdown, pages: 1) {
            case let .markdown(blocks, _, _):
                let leaves = blocks.reduce(0) { $0 + Self.renderedLeafCount($1) }
                let longest = blocks.reduce(0) { max($0, Self.maxTextLength($1)) }
                let deepest = blocks.reduce(0) { max($0, Self.nestingDepth($1)) }
                if leaves > maxLeaves || longest > maxText || deepest > maxDepth {
                    offenders.append(
                        "\(label): leaves=\(leaves) (max \(maxLeaves)), longestText=\(longest) (max \(maxText)), depth=\(deepest) (max \(maxDepth))"
                    )
                }
            case let .plain(text, _):
                if text.count > maxPlainChars {
                    offenders.append("\(label): plain text \(text.count) chars (max \(maxPlainChars))")
                }
            }
        }

        for shape in Self.adversarialShapes() {
            check(shape.name, shape.markdown)
        }
        for index in 0..<120 {
            let seed: UInt64 = 0x0AD_BEEF_C0FFEE &+ UInt64(index)
            var rng = SeededGenerator(seed: seed)
            let spec = FuzzedMessage.random(using: &rng)
            check("fuzz#\(index)[seed=0x\(String(seed, radix: 16))] \(spec.summary)", spec.markdown)
        }

        XCTAssertTrue(
            offenders.isEmpty,
            "Render plan is unbounded for some shapes (watchdog-hang risk):\n" + offenders.joined(separator: "\n")
        )
    }

    /// "Show more" grows the budget per page, so an unbounded page counter could
    /// rebuild the huge tree this change prevents. Verify the plan stays bounded
    /// at the page ceiling and beyond (the counter is clamped to `maxPages`).
    func testRenderPlanStaysBoundedAtAndBeyondMaxPages() {
        let maxLeaves = MarkdownRenderBudget.leafBudget * MarkdownRenderBudget.maxPages * 3
        let maxText = MarkdownRenderBudget.textCharCap * MarkdownRenderBudget.maxPages + 1
        let maxPlainChars = MarkdownRenderBudget.charsPerPage * MarkdownRenderBudget.maxPages
        var offenders: [String] = []
        for shape in Self.adversarialShapes() {
            for pages in [MarkdownRenderBudget.maxPages, MarkdownRenderBudget.maxPages + 100] {
                switch MarkdownRenderBudget.renderPlan(for: shape.markdown, pages: pages) {
                case let .markdown(blocks, _, _):
                    let leaves = blocks.reduce(0) { $0 + Self.renderedLeafCount($1) }
                    let longest = blocks.reduce(0) { max($0, Self.maxTextLength($1)) }
                    if leaves > maxLeaves || longest > maxText {
                        offenders.append("\(shape.name)@pages=\(pages): leaves=\(leaves) (max \(maxLeaves)), longestText=\(longest) (max \(maxText))")
                    }
                case let .plain(text, _):
                    if text.count > maxPlainChars {
                        offenders.append("\(shape.name)@pages=\(pages): plain \(text.count) chars (max \(maxPlainChars))")
                    }
                }
            }
        }
        XCTAssertTrue(
            offenders.isEmpty,
            "renderPlan grows without bound past maxPages (\"Show more\" could re-arm the watchdog):\n" + offenders.joined(separator: "\n")
        )
    }

    /// Truncation that paging can never reveal (table columns past
    /// maxTableColumns, nesting past maxNestingDepth) must be reported as
    /// permanent, so the view shows a static indicator instead of a no-op
    /// "Show more".
    func testPermanentTruncationIsReportedSeparately() {
        let wide = MarkdownShapes.bigTable(rows: 3, cols: MarkdownRenderBudget.maxTableColumns + 20)
        guard case let .markdown(_, _, widePermanent) = MarkdownRenderBudget.renderPlan(for: wide, pages: 1) else {
            return XCTFail("expected a markdown plan for a table")
        }
        XCTAssertTrue(widePermanent, "clipped table columns must be reported as permanent truncation")

        let deep = MarkdownShapes.nestedList(depth: MarkdownRenderBudget.maxNestingDepth + 20)
        guard case let .markdown(_, _, deepPermanent) = MarkdownRenderBudget.renderPlan(for: deep, pages: 1) else {
            return XCTFail("expected a markdown plan for a nested list")
        }
        XCTAssertTrue(deepPermanent, "collapsed deep nesting must be reported as permanent truncation")
    }

    /// The plain/markdown mode is decided once (first page), so paging never
    /// re-classifies a message — otherwise "Show more" could flip a plain message
    /// into the markdown path and shrink already-visible text.
    func testRenderModeIsMonotonicAcrossPages() {
        // First 16KB is plain prose (no markers); markdown appears only later.
        let mixed = String(repeating: "word ", count: 5000) + "\n\n## Heading\n\n- a\n- b\n"
        for pages in 1...MarkdownRenderBudget.maxPages {
            guard case .plain = MarkdownRenderBudget.renderPlan(for: mixed, pages: pages) else {
                return XCTFail("render mode flipped away from plain at page \(pages)")
            }
        }
    }

    /// Mirror of the renderer's leaf accounting, over the bounded block tree.
    private static func renderedLeafCount(_ block: NativeMarkdownBlock) -> Int {
        switch block {
        case .paragraph, .fallback, .heading, .thematicBreak, .codeBlock:
            return 1
        case .unorderedList(let items), .orderedList(_, let items):
            return items.reduce(0) { $0 + 1 + $1.blocks.reduce(0) { $0 + renderedLeafCount($1) } }
        case .blockQuote(let blocks):
            return max(1, blocks.reduce(0) { $0 + renderedLeafCount($1) })
        case .table(let header, let rows):
            return (rows.count + 1) * max(1, header.count)
        }
    }

    private static func nestingDepth(_ block: NativeMarkdownBlock) -> Int {
        switch block {
        case .paragraph, .fallback, .heading, .thematicBreak, .codeBlock, .table:
            return 1
        case .unorderedList(let items), .orderedList(_, let items):
            return 1 + items.reduce(0) { max($0, $1.blocks.reduce(0) { max($0, nestingDepth($1)) }) }
        case .blockQuote(let blocks):
            return 1 + blocks.reduce(0) { max($0, nestingDepth($1)) }
        }
    }

    private static func maxTextLength(_ block: NativeMarkdownBlock) -> Int {
        switch block {
        case .paragraph(let text), .fallback(let text):
            return text.count
        case .heading(_, let text):
            return text.count
        case .codeBlock(_, let code):
            return code.count
        case .thematicBreak:
            return 0
        case .unorderedList(let items), .orderedList(_, let items):
            return items.reduce(0) { max($0, $1.blocks.reduce(0) { max($0, maxTextLength($1)) }) }
        case .blockQuote(let blocks):
            return blocks.reduce(0) { max($0, maxTextLength($1)) }
        case .table(let header, let rows):
            let headerMax = header.reduce(0) { max($0, $1.count) }
            let rowsMax = rows.reduce(0) { max($0, $1.reduce(0) { max($0, $1.count) }) }
            return max(headerMax, rowsMax)
        }
    }

    // MARK: - Seeded fuzzer

    /// Randomly composes messages from the block generators and asserts each
    /// stays under budget. Deterministic (seeded) so any failure reproduces from
    /// the printed seed.
    func testFuzzedMessagesStayUnderBudget() {
        let baseSeed: UInt64 = 0x0AD_BEEF_C0FFEE
        let iterations = 50

        var offenders: [String] = []
        var report: [String] = []
        for index in 0..<iterations {
            let seed = baseSeed &+ UInt64(index)
            var rng = SeededGenerator(seed: seed)
            let spec = FuzzedMessage.random(using: &rng)
            let seconds = layoutSeconds(for: [assistantMessage(spec.markdown, id: "fuzz-\(index)")])
            if seconds > Self.budgetSeconds {
                offenders.append("seed=0x\(String(seed, radix: 16)) \(spec.summary) -> \(String(format: "%.3f", seconds))s")
                report.append("❌ seed=0x\(String(seed, radix: 16)) \(seconds.formatted())s :: \(spec.summary)")
            }
        }

        if !report.isEmpty {
            attach(report: report.joined(separator: "\n"), named: "layout-budget-fuzz-failures")
        }
        XCTAssertTrue(
            offenders.isEmpty,
            "Fuzzed messages exceeded the \(Self.budgetSeconds)s layout budget (each reproduces from its seed):\n" +
                offenders.joined(separator: "\n")
        )
    }

    // MARK: - Layout measurement

    /// Hosts the production message-list layout and forces a synchronous
    /// content-sizing pass, returning its wall-clock duration.
    private func layoutSeconds(for messages: [ChatMessage]) -> Double {
        let viewModel = ChatViewModel(authManager: AuthManager())
        let probe = ChatMessageListLayoutProbe(messages: messages, viewModel: viewModel)
        let host = UIHostingController(rootView: probe)
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: Self.width, height: Self.height))
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.view.frame = window.bounds

        let start = CFAbsoluteTimeGetCurrent()
        host.view.setNeedsLayout()
        host.view.layoutIfNeeded()
        _ = host.sizeThatFits(in: CGSize(width: Self.width, height: .greatestFiniteMagnitude))
        let elapsed = CFAbsoluteTimeGetCurrent() - start

        window.isHidden = true
        window.rootViewController = nil
        return elapsed
    }

    private func assistantMessage(_ markdown: String, id: String) -> ChatMessage {
        ChatMessage(
            id: id,
            role: .assistant,
            text: markdown,
            createdAt: Date(),
            toolCalls: [],
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil
        )
    }

    private func attach(report: String, named name: String) {
        let attachment = XCTAttachment(string: report)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

// MARK: - Content generators

/// Markdown shape generators. Each scales with its size argument so the harness
/// can drive a shape into the pathological regime.
enum MarkdownShapes {
    static func paragraphs(_ n: Int) -> String {
        (0..<n)
            .map { "Paragraph \($0) with **bold**, _italic_, and `code` that wraps across the bubble." }
            .joined(separator: "\n\n")
    }

    static func headings(_ n: Int) -> String {
        (0..<n).map { "## Heading \($0)\n\nBody text for section \($0)." }.joined(separator: "\n\n")
    }

    static func bigList(_ n: Int) -> String {
        (0..<n).map { "- item \($0) with a little wrapping text to measure" }.joined(separator: "\n")
    }

    static func nestedList(depth: Int) -> String {
        (0..<depth).map { String(repeating: "  ", count: $0) + "- level \($0)" }.joined(separator: "\n")
    }

    static func bigTable(rows: Int, cols: Int) -> String {
        let header = "| " + (0..<cols).map { "C\($0)" }.joined(separator: " | ") + " |"
        let separator = "| " + (0..<cols).map { _ in "---" }.joined(separator: " | ") + " |"
        let body = (0..<rows).map { r in
            "| " + (0..<cols).map { "r\(r)c\($0)" }.joined(separator: " | ") + " |"
        }
        return ([header, separator] + body).joined(separator: "\n")
    }

    static func giantCodeBlock(lines: Int) -> String {
        "```swift\n" + (0..<lines).map { "let value\($0) = compute(\($0), scale: \($0 * 2))" }.joined(separator: "\n") + "\n```"
    }

    static func longUnbrokenWord(_ length: Int) -> String {
        String(repeating: "A", count: length)
    }

    static func longInlineFormatting(_ n: Int) -> String {
        (0..<n).map { "**b\($0)** _i\($0)_ `c\($0)` [l\($0)](https://example.test)" }.joined(separator: " ")
    }

    static func everything(repeats: Int) -> String {
        let block = [
            "## Section",
            "A paragraph with **bold** and a [link](https://example.test).",
            "- one\n  - nested\n- two",
            "| A | B |\n| --- | --- |\n| 1 | 2 |",
            "```\ncode line\n```",
            "> a quote",
        ].joined(separator: "\n\n")
        return (0..<repeats).map { _ in block }.joined(separator: "\n\n")
    }
}

/// A randomly generated message spec for the fuzzer.
struct FuzzedMessage {
    let markdown: String
    let summary: String

    static func random(using rng: inout SeededGenerator) -> FuzzedMessage {
        let blockCount = Int.random(in: 1...6, using: &rng)
        var parts: [String] = []
        var kinds: [String] = []
        for _ in 0..<blockCount {
            switch Int.random(in: 0...7, using: &rng) {
            case 0:
                let n = Int.random(in: 1...2000, using: &rng)
                parts.append(MarkdownShapes.paragraphs(n)); kinds.append("paras(\(n))")
            case 1:
                let n = Int.random(in: 1...2000, using: &rng)
                parts.append(MarkdownShapes.bigList(n)); kinds.append("list(\(n))")
            case 2:
                let rows = Int.random(in: 1...600, using: &rng)
                let cols = Int.random(in: 1...60, using: &rng)
                parts.append(MarkdownShapes.bigTable(rows: rows, cols: cols)); kinds.append("table(\(rows)x\(cols))")
            case 3:
                let n = Int.random(in: 1...2500, using: &rng)
                parts.append(MarkdownShapes.giantCodeBlock(lines: n)); kinds.append("code(\(n))")
            case 4:
                let n = Int.random(in: 100...40_000, using: &rng)
                parts.append(MarkdownShapes.longUnbrokenWord(n)); kinds.append("word(\(n))")
            case 5:
                let n = Int.random(in: 1...2500, using: &rng)
                parts.append(MarkdownShapes.longInlineFormatting(n)); kinds.append("inline(\(n))")
            case 6:
                let d = Int.random(in: 1...40, using: &rng)
                parts.append(MarkdownShapes.nestedList(depth: d)); kinds.append("nested(\(d))")
            default:
                let n = Int.random(in: 1...2000, using: &rng)
                parts.append(MarkdownShapes.headings(n)); kinds.append("headings(\(n))")
            }
        }
        return FuzzedMessage(markdown: parts.joined(separator: "\n\n"), summary: kinds.joined(separator: "+"))
    }
}

/// Small deterministic PRNG (SplitMix64) so fuzz failures reproduce from a seed.
struct SeededGenerator: RandomNumberGenerator {
    private var state: UInt64

    init(seed: UInt64) {
        state = seed != 0 ? seed : 0x9E37_79B9_7F4A_7C15
    }

    mutating func next() -> UInt64 {
        state = state &+ 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
}
