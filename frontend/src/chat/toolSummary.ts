import { toolIconMapping } from './toolIconMapping';

const ARG_SUMMARY_MAX = 80;
const RESULT_PREVIEW_MAX = 240;

// Argument keys that, when present, describe the call better than anything else
// in it. Checked in order; the first one with a short scalar value wins.
const PRIMARY_ARG_KEYS = [
  'query',
  'sql',
  'title',
  'name',
  'url',
  'path',
  'prompt',
  'message',
  'text',
  'description',
  'target_service',
  'entity_id',
];

/**
 * A readable name for a tool: the curated label when there is one, otherwise
 * the snake_case name in sentence case ("query_trino" -> "Query trino").
 */
export function humanizeToolName(toolName: string | undefined | null): string {
  if (!toolName) {
    return 'Tool';
  }
  const curated = toolIconMapping[toolName]?.label;
  if (curated) {
    return curated;
  }
  const words = toolName
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .trim()
    .toLowerCase();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : toolName;
}

function truncate(text: string, max: number): string {
  const singleLine = text.replace(/\s+/g, ' ').trim();
  return singleLine.length > max ? `${singleLine.slice(0, max - 1)}…` : singleLine;
}

function scalarText(value: unknown): string | null {
  if (typeof value === 'string') {
    return value.trim() ? value : null;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return null;
}

function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, ' ').trim();
}

/**
 * One line describing what a call was asked to do, or null when the arguments
 * hold nothing a person would read (no arguments, or only nested structures).
 */
export function summarizeToolArgs(args: unknown): string | null {
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return null;
  }
  const record = args as Record<string, unknown>;

  for (const key of PRIMARY_ARG_KEYS) {
    const text = scalarText(record[key]);
    if (text) {
      return truncate(text, ARG_SUMMARY_MAX);
    }
  }

  const parts: string[] = [];
  for (const [key, value] of Object.entries(record)) {
    const text = scalarText(value);
    if (text) {
      parts.push(`${humanizeKey(key)}: ${text}`);
    }
    if (parts.length === 2) {
      break;
    }
  }
  return parts.length > 0 ? truncate(parts.join(' · '), ARG_SUMMARY_MAX) : null;
}

// Keys whose list is the substance of a result (as opposed to `columns` and
// other metadata that sit beside it).
const LIST_RESULT_KEYS = ['rows', 'results', 'items', 'data', 'events', 'notes', 'matches'];

function describeList(items: unknown[], noun: string): string {
  const scalars = items.map(scalarText);
  if (items.length > 0 && scalars.every((text) => text !== null)) {
    return truncate(scalars.join(', '), RESULT_PREVIEW_MAX);
  }
  const singular = noun.endsWith('s') ? noun.slice(0, -1) : noun;
  return items.length === 1 ? `1 ${singular}` : `${items.length} ${noun}`;
}

function describeStructure(value: unknown): string {
  if (Array.isArray(value)) {
    return describeList(value, 'items');
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const message = scalarText(record.error) ?? scalarText(record.message);
    if (message) {
      return truncate(message, RESULT_PREVIEW_MAX);
    }
    const entries = Object.entries(record).filter(([, entry]) => Array.isArray(entry));
    const listEntry =
      entries.find(([key]) => LIST_RESULT_KEYS.includes(key)) ??
      (entries.length === 1 ? entries[0] : undefined);
    if (listEntry) {
      return describeList(listEntry[1] as unknown[], humanizeKey(listEntry[0]));
    }
    const keys = Object.keys(record);
    return keys.length === 1 ? '1 field' : `${keys.length} fields`;
  }
  return String(value);
}

/**
 * A short, readable preview of a tool result. Plain text is clipped; JSON is
 * described by its shape rather than dumped. The full result stays available
 * behind the call's details.
 */
export function summarizeToolResult(result: unknown): string | null {
  if (result === undefined || result === null) {
    return null;
  }
  if (typeof result === 'string') {
    const trimmed = result.trim();
    if (!trimmed) {
      return null;
    }
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        return describeStructure(JSON.parse(trimmed));
      } catch {
        // Not JSON after all; preview it as text.
      }
    }
    return truncate(trimmed, RESULT_PREVIEW_MAX);
  }
  return describeStructure(result);
}

/** Pretty-printed raw text for the details view and the copy button. */
export function formatRawToolValue(value: unknown): string {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  return JSON.stringify(value, null, 2) ?? String(value);
}
