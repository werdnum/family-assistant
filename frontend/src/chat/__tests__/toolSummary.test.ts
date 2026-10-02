import { describe, expect, it } from 'vitest';
import {
  formatRawToolValue,
  humanizeToolName,
  summarizeToolArgs,
  summarizeToolResult,
} from '../toolSummary';

describe('humanizeToolName', () => {
  it('uses the curated label when one exists', () => {
    expect(humanizeToolName('search_calendar_events')).toBe('Search Events');
  });

  it('puts unknown snake_case names in sentence case', () => {
    expect(humanizeToolName('query_trino')).toBe('Query trino');
  });
});

describe('summarizeToolArgs', () => {
  it('prefers the argument that says what the call is about', () => {
    expect(summarizeToolArgs({ limit: 5, sql: 'SELECT 1' })).toBe('SELECT 1');
  });

  it('falls back to the first scalar arguments', () => {
    expect(
      summarizeToolArgs({
        start_date: '2026-10-01',
        days: 3,
        filters: { a: 1 },
      })
    ).toBe('start date: 2026-10-01 · days: 3');
  });

  it('collapses whitespace and truncates long values to one line', () => {
    const summary = summarizeToolArgs({
      query: `SELECT *\n  FROM t ${'x'.repeat(200)}`,
    });
    expect(summary).not.toContain('\n');
    expect(summary?.length).toBe(80);
    expect(summary?.endsWith('…')).toBe(true);
  });

  it('returns null when nothing is readable', () => {
    expect(summarizeToolArgs({})).toBeNull();
    expect(summarizeToolArgs({ nested: { a: 1 } })).toBeNull();
    expect(summarizeToolArgs(null)).toBeNull();
  });
});

describe('summarizeToolResult', () => {
  it('describes JSON results by shape instead of dumping them', () => {
    expect(summarizeToolResult('{"columns": ["a"], "rows": [[1], [2], [3]]}')).toBe('3 rows');
    expect(summarizeToolResult('[{"a": 1}, {"a": 2}]')).toBe('2 items');
    expect(summarizeToolResult('{"events": [{"id": 1}]}')).toBe('1 event');
    expect(summarizeToolResult({ a: 1, b: 2 })).toBe('2 fields');
  });

  it('lists short scalar lists inline', () => {
    expect(summarizeToolResult('{"notes": ["Test note", "Other"]}')).toBe('Test note, Other');
  });

  it('surfaces an error message from a JSON result', () => {
    expect(summarizeToolResult('{"error": "Table not found"}')).toBe('Table not found');
  });

  it('previews plain text', () => {
    expect(summarizeToolResult('Found 5 events')).toBe('Found 5 events');
    expect(summarizeToolResult('   ')).toBeNull();
    expect(summarizeToolResult(undefined)).toBeNull();
  });
});

describe('formatRawToolValue', () => {
  it('pretty-prints JSON strings and objects', () => {
    expect(formatRawToolValue('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(formatRawToolValue({ a: 1 })).toBe('{\n  "a": 1\n}');
  });

  it('leaves plain text alone', () => {
    expect(formatRawToolValue('hello')).toBe('hello');
  });
});
