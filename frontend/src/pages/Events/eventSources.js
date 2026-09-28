// Keyed by the backend's EventSourceType values (src/family_assistant/storage/events.py).
const EVENT_SOURCES = {
  home_assistant: { icon: '🏠', label: 'Home Assistant' },
  indexing: { icon: '📚', label: 'Indexing' },
  webhook: { icon: '🔗', label: 'Webhook' },
};

export const getSourceIcon = (sourceId) => EVENT_SOURCES[sourceId]?.icon ?? '📋';

export const getSourceLabel = (sourceId) =>
  EVENT_SOURCES[sourceId]?.label ?? (sourceId || 'Unknown');

export const EVENT_SOURCE_OPTIONS = Object.entries(EVENT_SOURCES).map(([id, { label }]) => ({
  id,
  label,
}));
