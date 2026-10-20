import type { ThreadSummary } from './types';

/** The caller's Home. `conversation_id` is null exactly when threads are disabled. */
export interface HomeResponse {
  enabled: boolean;
  conversation_id: string | null;
}

export interface HomeThreadsResponse {
  home_conversation_id: string | null;
  threads: ThreadSummary[];
}

export async function fetchHome(signal?: AbortSignal): Promise<HomeResponse> {
  const response = await fetch('/api/v1/chat/home', { signal });
  if (!response.ok) {
    throw new Error(`Failed to fetch Home: ${response.status}`);
  }
  return (await response.json()) as HomeResponse;
}

export async function fetchHomeThreads(signal?: AbortSignal): Promise<HomeThreadsResponse> {
  const response = await fetch('/api/v1/chat/home/threads', { signal });
  if (!response.ok) {
    throw new Error(`Failed to fetch threads: ${response.status}`);
  }
  return (await response.json()) as HomeThreadsResponse;
}

/** Close a thread as done on the user's say-so; returns the thread as it now stands. */
export async function markThreadDone(conversationId: string): Promise<ThreadSummary> {
  const response = await fetch(
    `/api/v1/chat/home/threads/${encodeURIComponent(conversationId)}/done`,
    { method: 'POST' }
  );
  if (!response.ok) {
    throw new Error(`Failed to mark the thread done: ${response.status}`);
  }
  return (await response.json()) as ThreadSummary;
}

/**
 * Move the caller's read marker in Home or a thread forward to
 * `throughMessageId`, or to its newest message when that is omitted; returns
 * how many messages are still unread.
 */
export async function markConversationRead(
  conversationId: string,
  throughMessageId?: number
): Promise<number> {
  const response = await fetch(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/read`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        throughMessageId === undefined ? {} : { through_message_id: throughMessageId }
      ),
    }
  );
  if (!response.ok) {
    throw new Error(`Failed to mark the conversation read: ${response.status}`);
  }
  const data = (await response.json()) as { unread_count: number };
  return data.unread_count;
}

/**
 * A thread action the server refused or that never reached it. `message` is
 * written for the user: the server's own reason where it gave one (a 409 says
 * why a move or a continuation can't happen), otherwise a plain description.
 */
export class ThreadActionError extends Error {
  /** The HTTP status, or null when the request never got an answer. */
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = 'ThreadActionError';
    this.status = status;
  }
}

/** An attachment sent with a thread's opening message, as on POST /v1/chat/turns. */
export interface ThreadAttachment {
  id: string;
  type: string;
  name: string;
  content: string;
}

export interface StartThreadRequest {
  profile_id: string;
  /** The user's opening message. Optional only when continuing with context. */
  prompt?: string;
  /** Identifies this start, so a retry returns the thread it already started. */
  turn_id: string;
  /** The Home message a "Continue in a thread" grows from. */
  anchor_message_id?: number;
  /** With an anchor: copy the anchor's turn and the Home turns before it. */
  include_context?: boolean;
  model_tier?: string;
  attachments?: ThreadAttachment[];
}

export interface StartThreadResponse {
  thread: ThreadSummary;
  /** The first turn, running the opening message; null when there is none. */
  turn: { turn_id: string; conversation_id: string; first_seq: number } | null;
  copied_message_ids: number[];
}

export interface MoveTurnResponse {
  thread: ThreadSummary;
  marker_message_id: number;
  moved_message_ids: number[];
}

async function refusalMessage(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (typeof body?.detail === 'string' && body.detail) {
    return body.detail;
  }
  if (response.status === 404) {
    return 'That is no longer available. Reload the page and try again.';
  }
  return `${fallback} (error ${response.status}).`;
}

async function postThreadAction<T>(url: string, body: unknown, fallback: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ThreadActionError(`${fallback}: the server couldn't be reached. Try again.`, null);
  }
  if (!response.ok) {
    throw new ThreadActionError(await refusalMessage(response, fallback), response.status);
  }
  return (await response.json()) as T;
}

/**
 * Start a thread of the caller's Home: "New thread", send-as-thread and
 * "Continue in a thread" all come through here.
 */
export function startHomeThread(request: StartThreadRequest): Promise<StartThreadResponse> {
  return postThreadAction<StartThreadResponse>(
    '/api/v1/chat/home/threads',
    request,
    "Couldn't start the thread"
  );
}

/** Move the whole Home turn holding `messageId` into a thread. */
export function moveTurnToThread(
  threadConversationId: string,
  messageId: number
): Promise<MoveTurnResponse> {
  return postThreadAction<MoveTurnResponse>(
    `/api/v1/chat/home/threads/${encodeURIComponent(threadConversationId)}/moved-turns`,
    { message_id: messageId },
    "Couldn't move the exchange"
  );
}
