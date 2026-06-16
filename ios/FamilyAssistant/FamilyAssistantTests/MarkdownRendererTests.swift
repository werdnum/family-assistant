import Markdown
import SwiftUI
import XCTest

@testable import FamilyAssistant

final class MarkdownRendererTests: XCTestCase {
    func testSwiftMarkdownParsesLinksCodeListsAndTables() throws {
        let markdown = """
        # Heading

        See [docs](https://example.test).

        - one
        - `two`

        ```swift
        let value = 1
        ```

        | Name | Value |
        | --- | --- |
        | A | B |
        """

        let document = Document(parsing: markdown)
        let debug = document.debugDescription()

        XCTAssertTrue(debug.contains("Heading"))
        XCTAssertTrue(debug.contains("Link"))
        XCTAssertTrue(debug.contains("InlineCode"))
        XCTAssertTrue(debug.contains("CodeBlock"))
        XCTAssertTrue(debug.contains("Table"))
    }

    func testMalformedMarkdownFallsBackToAttributedStringText() throws {
        let markdown = "[unterminated link"

        XCTAssertNoThrow(Document(parsing: markdown))
        let attributed = try AttributedString(markdown: markdown)
        XCTAssertTrue(String(attributed.characters).contains("unterminated link"))
    }

    func testNativeRendererProducesBlockNodes() throws {
        let markdown = """
        ## Heading

        See [docs](https://example.test) and `code`.

        - one
        - two

        ```swift
        let value = 1
        ```

        | Name | Value |
        | --- | --- |
        | A | B |

        > quoted
        """

        let blocks = NativeMarkdownRenderer.blocks(from: markdown)

        XCTAssertTrue(blocks.contains(.heading(level: 2, text: "Heading")))
        XCTAssertTrue(blocks.contains(.paragraph("See [docs](https://example.test) and `code`.")))
        XCTAssertTrue(blocks.contains(.unorderedList([
            NativeMarkdownListItem(checkbox: nil, blocks: [.paragraph("one")]),
            NativeMarkdownListItem(checkbox: nil, blocks: [.paragraph("two")]),
        ])))
        XCTAssertTrue(blocks.contains(.codeBlock(language: "swift", code: "let value = 1")), "\(blocks)")
        XCTAssertTrue(blocks.contains(.table(header: ["Name", "Value"], rows: [["A", "B"]])), "\(blocks)")
        XCTAssertTrue(blocks.contains(.blockQuote([.paragraph("quoted")])), "\(blocks)")
    }

    func testNativeRendererKeepsInlineMarkdownAttributes() throws {
        let attributed = try XCTUnwrap(
            NativeMarkdownRenderer.inlineAttributedString(from: "See [docs](https://example.test) and `code`.")
        )

        XCTAssertEqual(String(attributed.characters), "See docs and code.")
    }

    func testBlockParsingIsStableAcrossRepeatedCalls() throws {
        // Parsing is memoized; repeated calls for the same source must return the
        // same blocks, and a different source must parse independently.
        let source = """
        ## Title

        Some **bold** text with a `code` span.

        - a
        - b
        """

        let first = NativeMarkdownRenderer.blocks(from: source)
        let second = NativeMarkdownRenderer.blocks(from: source)
        XCTAssertEqual(first, second)
        XCTAssertEqual(NativeMarkdownRenderer.blocks(from: source + "\n\nmore"), NativeMarkdownRenderer.blocks(from: source + "\n\nmore"))
        XCTAssertNotEqual(first, NativeMarkdownRenderer.blocks(from: source + "\n\nmore"))
    }

    func testContainsMarkdownSyntaxTakesFastPathOnlyForPlainProse() {
        // Plain prose — even with numbers, hyphens mid-sentence, and punctuation —
        // takes the no-parse fast path.
        XCTAssertFalse(containsMarkdownSyntax("Just a plain sentence."))
        XCTAssertFalse(containsMarkdownSyntax("I have 3 cats and 2 dogs; that's 5 pets."))
        XCTAssertFalse(containsMarkdownSyntax("Line one\nLine two\nno markup here"))

        // Anything that could format must render through the parser.
        XCTAssertTrue(containsMarkdownSyntax("Some **bold** text"))
        XCTAssertTrue(containsMarkdownSyntax("An _italic_ word"))
        XCTAssertTrue(containsMarkdownSyntax("Inline `code` span"))
        XCTAssertTrue(containsMarkdownSyntax("A [link](https://example.test)"))
        XCTAssertTrue(containsMarkdownSyntax("| a | b |\n| - | - |"))
        XCTAssertTrue(containsMarkdownSyntax("# Heading"))
        XCTAssertTrue(containsMarkdownSyntax("> quote"))
        XCTAssertTrue(containsMarkdownSyntax("- bullet\n- list"))
        XCTAssertTrue(containsMarkdownSyntax("1. first\n2. second"))
        XCTAssertTrue(containsMarkdownSyntax("  - indented bullet"))
    }
}
