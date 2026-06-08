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
}
