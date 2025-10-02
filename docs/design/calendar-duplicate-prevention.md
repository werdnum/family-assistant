# Calendar Duplicate Prevention Design

**Status:** Proposed **Date:** 2025-10-01 **Author:** Claude (with user guidance)

## Problem Statement

The assistant frequently creates duplicate calendar events because:

1. LLM searches for exact event names, missing similar events
2. Example: Searches for "Doctor appointment" but misses "Dr. Smith checkup" at the same time
3. No server-side validation to catch duplicates
4. Current system prompt instruction ("always search first") is insufficient

## Experimental Results

We ran experiments comparing similarity measures on 20 realistic family calendar events with 5
variations each (100 comparisons total):

| Method                 | F1 Score  | Precision | Recall    | Speed (ms) | Size (MB) |
| ---------------------- | --------- | --------- | --------- | ---------- | --------- |
| **all-MiniLM-L6-v2**   | **0.888** | 0.806     | **0.988** | 54.88      | 86.6      |
| granite-30m-english    | 0.889     | 0.800     | 1.000     | 71.41      | 115.6     |
| Fuzzy String (difflib) | 0.843     | 0.814     | 0.875     | 0.03       | 0.0       |

**Key findings:**

- Embeddings dramatically improve semantic matching:
  - "Doctor appointment" → "Annual physical": Fuzzy=0.12, Embedding=0.60
  - "Doctor appointment" → "Dr. Smith checkup": Fuzzy=0.17, Embedding=0.70
- all-MiniLM-L6-v2 is optimal for production (30% faster than granite, nearly identical F1)
- Fuzzy matching is adequate for unit tests (zero dependencies, ~2000x faster)
- Optimal threshold: **0.30** for all methods (best F1 scores)

## Solution Architecture

### 1. Pluggable Similarity Strategy Pattern

Create an abstract `SimilarityStrategy` interface that allows different implementations:

```python
class SimilarityStrategy(Protocol):
    """Protocol for computing similarity between calendar event titles."""

    async def compute_similarity(self, title1: str, title2: str) -> float:
        """
        Compute similarity score between two event titles.

        Returns:
            float: Similarity score between 0.0 and 1.0
        """
        ...

    @property
    def name(self) -> str:
        """Name of this similarity strategy."""
        ...
```

**Implementations:**

1. **FuzzySimilarityStrategy** - For unit tests and lightweight deployments

   - Uses `difflib.SequenceMatcher`
   - Zero dependencies
   - Fast (0.03ms per comparison)
   - F1=0.843

2. **EmbeddingSimilarityStrategy** - For production with local models

   - Uses sentence-transformers with configurable model
   - Default: `sentence-transformers/all-MiniLM-L6-v2`
   - Requires `local-embeddings` extra (~87MB)
   - F1=0.888, 98.8% recall

3. **CloudEmbeddingSimilarityStrategy** (future) - For cloud API providers

   - Uses existing LiteLLM integration
   - Configurable via config.yaml
   - Supports OpenAI, Anthropic, etc.

### 2. Enhanced Search Tool

**Modify `search_calendar_events_tool` to support similarity-based search:**

```python
async def search_calendar_events_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    search_text: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    similarity_threshold: float = 0.30,  # NEW
) -> str:
    """
    Searches for calendar events by summary text or within a date range.

    NEW BEHAVIOR:
    - When search_text is provided, returns events with similarity >= threshold
    - Results include similarity scores for each event
    - Sorted by: time proximity first, then similarity score
    - Uses configured SimilarityStrategy (embedding or fuzzy)
    """
```

**Example output:**

```
Found 3 event(s):

1. Dr. Smith checkup (similarity: 0.70)
   Start: Tomorrow 14:00
   End: Tomorrow 14:30
   UID: abc-123
   Calendar: https://...

2. Medical appointment (similarity: 0.94)
   Start: Tomorrow 14:15
   End: Tomorrow 15:00
   UID: def-456
   Calendar: https://...

3. Dentist appointment (similarity: 0.89)
   Start: Tomorrow 16:00
   End: Tomorrow 16:30
   UID: ghi-789
   Calendar: https://...
```

### 3. Server-Side Duplicate Detection

**Add validation to `add_calendar_event_tool` BEFORE creating event:**

```python
async def add_calendar_event_tool(
    exec_context: ToolExecutionContext,
    calendar_config: dict[str, Any],
    summary: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    all_day: bool = False,
    recurrence_rule: str | None = None,
    bypass_duplicate_check: bool = False,  # NEW: Allow override
) -> str:
    """
    Adds an event to the calendar.

    NEW BEHAVIOR:
    1. BEFORE creation, search for similar events in time window:
       - Timed events: ±2 hours
       - All-day events: same date
    2. If similar events found (similarity >= threshold) AND bypass_duplicate_check=False:
       - Return error message with duplicate details
       - Instruct LLM how to retry with bypass flag
    3. If bypass_duplicate_check=True OR no duplicates found:
       - Create the event as requested
    """
```

**Error response format (first attempt):**

```
Error: Cannot create event 'Doctor appointment' - found 2 similar event(s) at nearby times:

1. 'Dr. Smith checkup' at Tomorrow 14:00 (similarity: 0.70)
   UID: abc-123

2. 'Medical appointment' at Tomorrow 14:15 (similarity: 0.94)
   UID: def-456

If you believe this is NOT a duplicate, retry with bypass_duplicate_check=true.
```

**Success response format (with bypass):**

```
OK. Event 'Doctor appointment' added to the calendar (duplicate check bypassed).
```

**This is a PRE-WARNING with bypass** - event creation blocked on first attempt, but LLM can
override if it determines the event is not actually a duplicate. This prevents spurious
notifications to shared calendar users while still allowing legitimate events with similar names.

### 4. Configuration

**Add to config.yaml:**

```yaml
calendar:
  caldav:
    # ... existing caldav config ...

  # NEW: Duplicate detection settings
  duplicate_detection:
    enabled: true
    similarity_strategy: "embedding"  # Options: "embedding", "fuzzy"
    similarity_threshold: 0.30
    time_window_hours: 2  # For timed events

    # Only used if similarity_strategy == "embedding"
    embedding:
      model: "sentence-transformers/all-MiniLM-L6-v2"
      device: "cpu"  # or "cuda", "mps"
```

**For unit tests, override in test config:**

```yaml
calendar:
  duplicate_detection:
    similarity_strategy: "fuzzy"  # Fast, zero dependencies
```

### 5. System Prompt Enhancement

**Update prompts.yaml (line ~58):**

```yaml
# OLD:
* Before adding calendar events, always search existing events first to avoid duplicates.
  Use the `search_calendar_events` tool to check if similar events already exist before
  calling `add_calendar_event`. This prevents cluttering the calendar with duplicate entries.

# NEW:
* Before adding calendar events, ALWAYS search to avoid duplicates:
  1. Search with BROAD terms (e.g., "doctor" not "doctor appointment with Dr. Smith")
  2. Review ALL results - search shows semantically similar events, not just exact matches
  3. Each result includes a similarity score (0.0-1.0) showing how close the match is
  4. If an event exists at the same time with high similarity (>0.7), it's likely a duplicate

  If add_calendar_event returns an error about similar events, review the similar events
  and decide if this is truly a duplicate. If it's NOT a duplicate (e.g., different doctors,
  different purposes), retry with bypass_duplicate_check=true to create the event anyway.
```

## Implementation Plan

### Phase 1: Core Infrastructure

1. **Create similarity strategy module** (`src/family_assistant/similarity.py`)

   - Define `SimilarityStrategy` protocol
   - Implement `FuzzySimilarityStrategy`
   - Implement `EmbeddingSimilarityStrategy`

2. **Add configuration support**

   - Update config loading to read `duplicate_detection` settings
   - Add factory function to create strategy from config

### Phase 2: Tool Integration

3. **Enhance search tool**

   - Add `similarity_threshold` parameter
   - Inject `SimilarityStrategy` via execution context
   - Compute similarity for each event
   - Add similarity scores to output

4. **Add duplicate detection**

   - Create helper function `_check_for_duplicate_events()`
   - Call BEFORE event creation in `add_calendar_event_tool`
   - Return error message with conflict details if duplicates found
   - Add `bypass_duplicate_check` parameter to allow override

### Phase 3: Testing & Documentation

5. **Unit tests**

   - Test both similarity strategies
   - Test search with similarity scoring
   - Test duplicate detection (should block first attempt)
   - Test `bypass_duplicate_check=true` override (should succeed)
   - Use `FuzzySimilarityStrategy` for speed

6. **Integration tests**

   - End-to-end with mock LLM
   - Verify LLM can't create duplicates
   - Verify override mechanism works

7. **Update documentation**

   - Update USER_GUIDE.md with duplicate detection explanation
   - Document configuration options

## Thresholds & Time Windows

Based on experimental data:

**Similarity Thresholds:**

- **Search tool:** 0.30 (high recall, LLM can review candidates)
- **Duplicate detection:** 0.30 (blocks at same threshold to be conservative)
  - F1=0.888 at this threshold
  - Precision=0.806 (19.4% false positive rate)
  - Recall=0.988 (catches 98.8% of duplicates)

**Time Windows:**

- Timed events: ±2 hours (only check for duplicates within this window)
- All-day events: same date

**Rationale for 0.30 threshold:**

- Maximizes F1 score for all methods
- Higher recall (98.8%) is more important than precision for duplicate detection
- False positives are mitigated by `allow_similar` override
- User can see similarity scores and make informed decision

## Trade-offs

### Accepted:

1. **19.4% false positive rate** - Some distinct events flagged as similar

   - Mitigated by: Warning (not hard block), similarity scores visible to LLM
   - LLM can make final judgment call
   - Better than missing 1.2% of duplicates with higher threshold

2. **Event blocked on first attempt** - Error shown BEFORE creation

   - Tradeoff: Requires LLM to retry with bypass flag if not a duplicate
   - Benefit: Prevents spurious notifications to shared calendar users
   - Better UX for shared calendars than creating then deleting

3. **Extra latency** - 55ms per similarity comparison (embedding mode)

   - Typical duplicate check: 1-5 events in time window = 55-275ms overhead
   - Runs BEFORE event creation, blocks creation until check completes
   - Acceptable for calendar operations (not high-frequency)

4. **Memory footprint** - 87MB for all-MiniLM-L6-v2 model

   - Only loaded if `similarity_strategy: "embedding"`
   - Unit tests use fuzzy matching (0 MB)

5. **Configuration complexity** - Multiple strategy options

   - Mitigated by: sensible defaults, clear documentation
   - Most users never need to change defaults

### Not Accepted:

1. **Cloud API costs** - Not implementing `CloudEmbeddingSimilarityStrategy` yet

   - Local embedding model is sufficient
   - Can add later if needed

2. **int8 quantization** - Requires additional `optimum` dependency

   - Marginal size/speed improvement (~30% smaller, ~20% faster)
   - Adds complexity, not worth it for 87MB model

## Validation

After implementation, test against these scenarios:

**Should BLOCK:**

- ✅ "Doctor appointment" finds "Dr. Smith checkup" at same time
- ✅ "Soccer practice" finds "Kids football training" at same time
- ✅ "Grocery shopping" finds "Food shopping" at same time

**Should NOT block:**

- ❌ "Doctor appointment" vs "Dentist appointment" (different specialists, similarity ~0.89)
  - **Note:** This may actually block! False positive acceptable with override.
- ❌ "Soccer practice" vs "Swimming practice" (different sports)
- ❌ Same title but different day/time (outside time window)

## Future Enhancements

1. **Cloud embedding strategy** - Use LiteLLM for cloud APIs
2. **Smarter time windows** - ML-based prediction of likely duplicates
3. **User-specific thresholds** - Learn from user override patterns
4. **Batch similarity** - Compute all similarities in one model pass (faster)

## Dependencies

- **Core:** No new dependencies (difflib is stdlib)
- **Production embedding:** sentence-transformers, torch (~100MB extra via `local-embeddings` extra)
- **Already installed:** Project already has `local-embeddings` as optional extra

## Questions for Review

1. **Threshold tuning:** Is 0.30 appropriate, or should we be more/less aggressive?
2. **Time windows:** Are ±2 hours (timed) and same-day (all-day) reasonable?
3. ~~**Hard block vs warning:**~~ **RESOLVED** - Use warning (not hard block) to ensure assistant
   knows there might be a duplicate
4. **Strategy naming:** `FuzzySimilarityStrategy` vs `DifflibSimilarityStrategy`?
5. **Config location:** Should this be under `calendar.duplicate_detection` or top-level
   `duplicate_detection`?
