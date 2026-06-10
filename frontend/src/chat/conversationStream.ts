// Shared helpers for the resumable conversation stream endpoint. Both the
// send-and-watch flow (useStreamingResponse) and the always-on live-update
// listener (useLiveMessageUpdates) talk to the same
// `GET /api/v1/chat/conversations/{id}/stream` endpoint and previously each
// hand-built the URL and the 401 redirect; keep that shape in one place.

export interface ConversationStreamParams {
  fromSeq?: number;
  follow?: boolean;
  ackSeq?: number;
  /** Comma-separated allow-list of event types to receive (e.g.
   * `message,turn_ended`). Lifecycle frames (turn_ended, heartbeat,
   * stream_dropped) are always delivered regardless. Follow streams use this
   * to skip the token firehose they don't render. */
  eventTypes?: string;
}

/** Build the SSE subscribe URL for a conversation. Returns a root-relative
 * path with query string; EventSource and fetch both resolve it correctly. */
export function conversationStreamUrl(
  conversationId: string,
  { fromSeq, follow, ackSeq, eventTypes }: ConversationStreamParams = {}
): string {
  const params = new URLSearchParams();
  if (fromSeq !== undefined) {
    params.set('from_seq', String(fromSeq));
  }
  if (follow !== undefined) {
    params.set('follow', follow ? 'true' : 'false');
  }
  if (ackSeq !== undefined) {
    params.set('ack_seq', String(ackSeq));
  }
  if (eventTypes !== undefined) {
    params.set('event_types', eventTypes);
  }
  const query = params.toString();
  const base = `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/stream`;
  return query ? `${base}?${query}` : base;
}

/** Redirect to the login page, preserving the current location as `next`. */
export function redirectToLogin(): void {
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/login?next=${next}`;
}
