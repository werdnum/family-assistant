# Documents and Search

**What's here:** getting documents into the assistant's knowledge base, searching everything it has
stored, and reading a document in full.

## Searching

The assistant searches across notes, indexed emails, PDFs, uploaded files, and indexed web pages,
combining semantic matching with keyword matching:

- "Search my notes for 'plumber number'."
- "Find emails about the flight booking."
- "Look for PDF documents related to 'insurance policy'."

Results are grouped by document, with the relevant snippets shown. You can narrow by source type
("search only my emails") when you know where something lives.

In the web interface, the **Vector Search** page searches the same material and lets you click
through to a document, but it matches on meaning only. If you're hunting an exact phrase, a
reference number, or a name, ask the assistant instead — it matches wording as well as meaning, so
it will find an exact string the page can miss.

## Reading a full document

Search results identify documents by ID ("Document ID 123: Insurance Policy Scan"). Ask for the
whole thing:

- "Show me the full content of document 123."

In the web interface, clicking a search result opens the document detail view, which shows the
complete text at the top along with metadata and the matching snippets. Large documents are readable
in full even when they were too big to index completely for search.

## Adding documents

### From a URL

- "Save this page for later: `https://example.com/article`"
- "Index this article: `https://example.com/article` with title 'My Article Title'"

Give the full address including `https://`. If you don't supply a title, the assistant extracts one
from the page.

### From a file

Upload PDFs, text files, and other documents on the **Upload Document** page of the web interface.
The assistant processes and indexes them so their content becomes searchable.

Sending a file in a chat message is different: it becomes an attachment the assistant can read and
work with for that conversation, but it is not added to your searchable knowledge base. To keep a
file permanently, upload it on the **Upload Document** page. See [attachments.md](attachments.md)
for what the assistant can do with files in a conversation.

### From email

Forwarded email is indexed, including real attachments. Documents linked from an email are fetched
only after you approve it. See [email.md](email.md).

## Searching past conversations

You can ask about your own conversation history rather than documents:

- "What did I say about passports last month?"
- "When did you last add a calendar event for me?"

The assistant can filter on exact fields — dates, roles, tools used, attachments, errors — and can
also search semantically for fuzzy references. Results are scoped to your own history.

The **History** page in the web interface shows the same conversations across Telegram, web, and
email, with filtering.

Semantic search over a conversation catches up a couple of minutes after the exchange finishes, so a
reply you just received may not be findable by a fuzzy description straight away. Exact-field
filtering and the History page see it immediately.

## Troubleshooting

- **A document isn't turning up in search.** Indexing runs in the background; give a large upload a
  moment. Check the **Documents** page to confirm it arrived.
- **You know the document exists but search misses it.** Try naming it more specifically, or search
  the **Documents** page directly and open it by ID.
- **An email's attachments show no attachment ID.** That email was indexed before attachment
  tracking; ask the assistant to reindex it and the attachments become available.
