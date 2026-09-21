import Foundation
import Markdown

/// The only file in the app target that imports swift-markdown.
///
/// `Markdown.Text` collides with `SwiftUI.Text`, and for a `String`-typed
/// argument the non-generic `Markdown.Text(_: String)` is the better overload,
/// so in a file importing both, `Text(someString)` can resolve to the markup
/// node and fail with "requires that 'Text' conform to 'View'" — which toolchain
/// picks which varies. Parsing therefore lives here and hands the views a model
/// of its own, so no SwiftUI file has to import Markdown.

/// A checkbox on a markdown list item, independent of the parser's type so that
/// rendering code need not import swift-markdown.
enum MarkdownCheckbox: Equatable {
    case checked
    case unchecked

    init(_ checkbox: Checkbox) {
        switch checkbox {
        case .checked:
            self = .checked
        case .unchecked:
            self = .unchecked
        }
    }
}

indirect enum NativeMarkdownBlock: Equatable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case unorderedList([NativeMarkdownListItem])
    case orderedList(startIndex: UInt, items: [NativeMarkdownListItem])
    case blockQuote([NativeMarkdownBlock])
    case codeBlock(language: String?, code: String)
    case table(header: [String], rows: [[String]])
    case thematicBreak
    case fallback(String)
}

struct NativeMarkdownListItem: Equatable {
    let checkbox: MarkdownCheckbox?
    let blocks: [NativeMarkdownBlock]
}

enum NativeMarkdownRenderer {
    // Parsing markdown is expensive, and SwiftUI re-evaluates a bubble's body
    // far more often than its text changes (scrolling a long thread, sibling
    // updates, layout passes). Caching by exact source string keeps a given
    // message from being re-parsed on every render. Parsing is pure, so caching
    // by the source string is always correct.
    private final class BlocksBox {
        let blocks: [NativeMarkdownBlock]
        init(_ blocks: [NativeMarkdownBlock]) { self.blocks = blocks }
    }

    private final class AttributedBox {
        let value: AttributedString?
        init(_ value: AttributedString?) { self.value = value }
    }

    private static let blockCache: NSCache<NSString, BlocksBox> = {
        let cache = NSCache<NSString, BlocksBox>()
        cache.countLimit = 256
        return cache
    }()

    private static let inlineCache: NSCache<NSString, AttributedBox> = {
        let cache = NSCache<NSString, AttributedBox>()
        cache.countLimit = 512
        return cache
    }()

    static func blocks(from markdown: String) -> [NativeMarkdownBlock] {
        let key = markdown as NSString
        if let cached = blockCache.object(forKey: key) {
            return cached.blocks
        }
        let document = Document(parsing: markdown)
        let parsed = document.children.flatMap(blocks(from:))
        let result = parsed.isEmpty ? [.paragraph(markdown)] : parsed
        blockCache.setObject(BlocksBox(result), forKey: key)
        return result
    }

    static func inlineAttributedString(from markdown: String) -> AttributedString? {
        let key = markdown as NSString
        if let cached = inlineCache.object(forKey: key) {
            return cached.value
        }
        let value = try? AttributedString(
            markdown: markdown,
            options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
        inlineCache.setObject(AttributedBox(value), forKey: key)
        return value
    }

    private static func blocks(from markup: Markup) -> [NativeMarkdownBlock] {
        switch markup {
        case let heading as Heading:
            return [.heading(level: heading.level, text: heading.plainText)]
        case let paragraph as Paragraph:
            return [.paragraph(inlineMarkdown(from: paragraph))]
        case let unorderedList as UnorderedList:
            return [.unorderedList(unorderedList.listItems.map(listItem(from:)))]
        case let orderedList as OrderedList:
            return [.orderedList(startIndex: orderedList.startIndex, items: orderedList.listItems.map(listItem(from:)))]
        case let blockQuote as BlockQuote:
            return [.blockQuote(blockQuote.children.flatMap(blocks(from:)))]
        case let codeBlock as CodeBlock:
            return [.codeBlock(language: codeBlock.language, code: codeBlock.code.trimmingCharacters(in: CharacterSet.newlines))]
        case let table as Markdown.Table:
            return [.table(header: table.head.cells.map { $0.plainText }, rows: table.body.rows.map { $0.cells.map { $0.plainText } })]
        case is ThematicBreak:
            return [.thematicBreak]
        default:
            let fallback = inlineMarkdown(from: markup)
            return fallback.isEmpty ? [] : [.fallback(fallback)]
        }
    }

    private static func listItem(from item: ListItem) -> NativeMarkdownListItem {
        NativeMarkdownListItem(checkbox: item.checkbox.map(MarkdownCheckbox.init), blocks: item.children.flatMap(blocks(from:)))
    }

    private static func inlineMarkdown(from markup: Markup) -> String {
        var formatter = MarkupFormatter()
        formatter.visit(markup.detachedFromParent)
        return formatter.result.trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
    }
}
