# Design Doc: Document Protocol and SQLAlchemy Model Metadata Conflict

**Date:** June 24, 2025
**Author:** Gemini Assistant
**Status:** In Review

## 1. Summary

A latent conflict exists between the `Document` protocol and its primary implementation, the `DocumentRecord` SQLAlchemy model. The protocol specifies a `metadata` property, but the model must use `doc_metadata` for the corresponding database column because `metadata` is a reserved keyword in SQLAlchemy.

This conflict was surfaced during the implementation of the "Re-index Document" feature, which required accessing this metadata field on an object typed only as the `Document` protocol, causing a `basedpyright` type-checking error: `Cannot access attribute "doc_metadata" for class "Document"`.

This document proposes a pragmatic short-term fix to unblock the feature and outlines potential long-term solutions for discussion.

## 2. Context: The Re-indexing Feature

The goal was to add a "Re-index" button to the Documents UI. This feature required a mechanism to signal to the indexing pipeline that it should forcefully re-process certain parts of a document, such as re-extracting its title.

The chosen implementation was to add a `force_title_update: true` flag to the document's metadata. The `DocumentTitleUpdaterProcessor` was modified to check for this flag on the `original_document` object it receives.

The `process` method signature for this processor is:

```python
async def process(
    self,
    current_items: list[IndexableContent],
    original_document: Document,  # Type-hinted as the protocol
    ...
) -> list[IndexableContent]:
```

## 3. Root Cause Analysis

The type error arose from the following conditions:

1.  **The Protocol Definition:** The `Document` protocol in `src/family_assistant/storage/vector.py` defines the interface for document-like objects and includes this property:

    ```python
    @property
    def metadata(self) -> dict[str, Any] | None: ...
    ```

2.  **The SQLAlchemy Implementation:** The `DocumentRecord` ORM model in the same file implements this protocol. However, due to a name collision with the reserved `metadata` attribute in SQLAlchemy's declarative base, the actual column is named `doc_metadata`:

    ```python
    class DocumentRecord(Base):
        __tablename__ = "documents"
        # ...
        doc_metadata: Mapped[dict[str, Any] | None] = mapped_column(...)
    ```

3.  **The Catalyst:** The re-indexing feature introduced the first piece of code that attempts to access the `doc_metadata` field on the `original_document` object from within the `DocumentTitleUpdaterProcessor`:

    ```python
    # Inside DocumentTitleUpdaterProcessor.process
    if (
        hasattr(original_document, "doc_metadata")
        and original_document.doc_metadata  # <-- This line causes the type error
        and original_document.doc_metadata.get("force_title_update")
    ):
        ...
    ```

The static type checker, seeing an object of type `Document` (the protocol), correctly reports that `doc_metadata` is not a defined attribute of that interface, even though at runtime the object is a `DocumentRecord` which does have that attribute.

## 4. Short-Term Solution (Immediate Fix)

To unblock the current feature without introducing large-scale, potentially disruptive changes, the recommended approach is to acknowledge the type checker's limitation in this specific context.

We will add a `# type: ignore[attr-defined]` comment to the line causing the error:

```python
# In src/family_assistant/indexing/processors/metadata_processors.py

if (
    hasattr(original_document, "doc_metadata")
    and original_document.doc_metadata  # type: ignore[attr-defined]
    and original_document.doc_metadata.get("force_title_update")
):
    self.config.force_update = True
```

**Pros:**

*   Extremely low-risk and localized.
*   Unblocks the feature immediately.
*   Clearly flags the issue for future resolution.

**Cons:**

*   It is a temporary workaround and incurs technical debt.

## 5. Proposed Long-Term Solutions

The underlying architectural conflict should be resolved properly. The following options should be considered:

### Option A: Align the Protocol with the Implementation

-   **Action:** Rename the property in the `Document` protocol from `metadata` to `doc_metadata`.
-   **Impact:** This would be the most "correct" fix from a type-safety perspective. However, it would require a broad refactoring effort to update all other implementations of the protocol (e.g., mock objects in over a dozen test files) and any code that currently accesses `.metadata`. This was attempted and found to be highly invasive for a seemingly small issue.

### Option B: Use `typing.cast`

-   **Action:** Instead of ignoring the type error, use `typing.cast` to explicitly tell the type checker to treat the object as a `DocumentRecord`.

    ```python
    from typing import cast
    from family_assistant.storage.vector import DocumentRecord

    # ...
    doc_record = cast(DocumentRecord, original_document)
    if doc_record.doc_metadata and doc_record.doc_metadata.get("force_title_update"):
        ...
    ```

-   **Impact:** This is safer than a bare `type: ignore` as it makes the developer's assumption explicit. It introduces a new import and is slightly more verbose.

### Option C: Add `doc_metadata` to the Protocol

-   **Action:** Add `doc_metadata` as an optional property to the `Document` protocol, alongside the existing `metadata` property.
-   **Impact:** This would resolve the type error but could be confusing, as the protocol would have two very similar properties. It might be the most pragmatic of the "real" fixes.

## 6. Recommendation

1.  **Immediately:** Implement the **Short-Term Solution** to complete the re-indexing feature.
2.  **Future Work:** Schedule a follow-up task to discuss and implement one of the long-term solutions, with a preference for **Option B (using `cast`)** as it offers the best balance of safety and low-impact implementation.
