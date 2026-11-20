import { LOADING_MARKER } from './constants';
import { interleaveTextAndToolCalls, isToolCallDisplay } from './orderedParts';
import { isTerminalToolOutcome, type TerminalToolOutcome } from './toolOutcome';
import type {
  ActiveTurnInfo,
  BackendAttachment,
  BackendConversationMessage,
  Message,
  MessageContent,
} from './types';
import type { PendingDelegation } from './usePendingDelegations';

export interface SubconversationMessagesResponse {
  conversation_id: string;
  subconversation_id: string;
  delegation: PendingDelegation | null;
  delegation_error: string | null;
  request_text: string | null;
  child_delegations: PendingDelegation[];
  messages: BackendConversationMessage[];
  count: number;
  total_messages: number;
  has_more_before: boolean;
  has_more_after: boolean;
  active_turns: ActiveTurnInfo[];
}

export interface LiveToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  argsText: string;
  result?: string;
  outcome?: TerminalToolOutcome;
  display?: MessageContent['display'];
  attachments?: BackendAttachment[];
  textOffset: number;
}

export interface LiveSubconversationTurn {
  turnId: string;
  text: string;
  toolCalls: LiveToolCall[];
  attachments: BackendAttachment[];
  profileId?: string;
  startedAt: Date;
  error?: string;
}

function parseToolArguments(argumentSource: unknown): Record<string, unknown> {
  if (argumentSource && typeof argumentSource === 'object' && !Array.isArray(argumentSource)) {
    return argumentSource as Record<string, unknown>;
  }
  if (typeof argumentSource === 'string' && argumentSource.trim()) {
    try {
      const parsed: unknown = JSON.parse(argumentSource);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return { raw: argumentSource };
    }
  }
  return {};
}

function normalizeAttachment(att: BackendAttachment): BackendAttachment {
  const url =
    (typeof att.content_url === 'string' && att.content_url) ||
    (typeof att.url === 'string' && att.url) ||
    (att.attachment_id ? `/api/attachments/${att.attachment_id}` : undefined);
  return {
    ...att,
    content_url: url,
    url,
    name:
      att.name ||
      (typeof att.description === 'string' ? att.description : undefined) ||
      'Attachment',
  };
}

function extractText(content: BackendConversationMessage['content']): string {
  if (typeof content === 'string') {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((part) => (part.type === 'text' && typeof part.text === 'string' ? part.text : null))
      .filter((value): value is string => value !== null)
      .join('\n\n');
  }
  return '';
}

function isToolOnlyAssistantRow(msg: Message): boolean {
  if (msg.role !== 'assistant') {
    return false;
  }
  const hasTools = msg.content.some((part) => part.type === 'tool-call');
  const hasText = msg.content.some(
    (part) => part.type === 'text' && Boolean(part.text && part.text.trim())
  );
  return hasTools && !hasText;
}

function sameTurn(a: Message, b: Message): boolean {
  return !a.turnId || !b.turnId || a.turnId === b.turnId;
}

/**
 * Convert persisted subconversation rows into the `Message[]` shape rendered by
 * `<Thread />`. Mirrors `ConversationPane`'s history conversion while omitting
 * persisted assistant/tool rows for turns actively replaying from the live hub.
 */
export function buildSubconversationMessages(
  rows: BackendConversationMessage[],
  runningTurnIds: Set<string>,
  replayingTurnIds: Set<string>
): Message[] {
  const toolResponses = new Map<string, string>();
  const toolOutcomes = new Map<string, TerminalToolOutcome>();
  const toolAttachments = new Map<string, BackendAttachment[]>();

  for (const msg of rows) {
    if (msg.role !== 'tool' || !msg.tool_call_id) {
      continue;
    }
    toolResponses.set(msg.tool_call_id, extractText(msg.content));
    if (isTerminalToolOutcome(msg.tool_outcome)) {
      toolOutcomes.set(msg.tool_call_id, msg.tool_outcome);
    }
    if (Array.isArray(msg.attachments) && msg.attachments.length > 0) {
      toolAttachments.set(msg.tool_call_id, msg.attachments);
    }
  }

  const processed: Message[] = [];
  for (const msg of rows) {
    if (msg.role === 'tool') {
      continue;
    }
    if (msg.role !== 'user' && msg.turn_id && replayingTurnIds.has(msg.turn_id)) {
      continue;
    }

    if (msg.role === 'error') {
      const errorText =
        extractText(msg.content).trim() || 'An error occurred in this subconversation.';
      processed.push({
        id: `sub_msg_${msg.internal_id}`,
        role: 'assistant',
        turnId: msg.turn_id ?? undefined,
        content: [{ type: 'text', text: errorText }],
        createdAt: new Date(msg.timestamp),
        status: { type: 'complete' },
        internalId: Number(msg.internal_id),
        processing_profile_id: msg.processing_profile_id ?? undefined,
      });
      continue;
    }

    if (msg.role === 'assistant') {
      const content: MessageContent[] = [];
      const text = extractText(msg.content);
      if (text) {
        content.push({ type: 'text', text });
      }

      const allAttachments: BackendAttachment[] = [];
      const allAttachmentIds: string[] = [];
      const appendAttachments = (items: BackendAttachment[] | undefined) => {
        if (!Array.isArray(items) || items.length === 0) {
          return;
        }
        for (const item of items) {
          allAttachments.push(normalizeAttachment(item));
          if (typeof item.attachment_id === 'string') {
            allAttachmentIds.push(item.attachment_id);
          }
        }
      };

      if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
        for (const toolCall of msg.tool_calls) {
          const toolResponse = toolResponses.get(toolCall.id);
          const toolName = toolCall.function?.name || toolCall.name || 'unknown';
          const argumentSource = toolCall.function?.arguments ?? toolCall.arguments;
          const args = parseToolArguments(argumentSource);
          const argsText =
            typeof argumentSource === 'string' ? argumentSource : JSON.stringify(args);

          content.push({
            type: 'tool-call',
            toolCallId: toolCall.id,
            toolName,
            args,
            argsText,
            result: toolResponse,
            outcome: toolOutcomes.get(toolCall.id),
            awaitingResult:
              toolResponse === undefined && Boolean(msg.turn_id && runningTurnIds.has(msg.turn_id)),
            display: isToolCallDisplay(toolCall.display) ? toolCall.display : undefined,
          });
          appendAttachments(toolAttachments.get(toolCall.id));
        }
      }

      appendAttachments(msg.metadata?.attachments);
      appendAttachments(msg.attachments);

      if (allAttachments.length > 0) {
        content.push({
          type: 'tool-call',
          toolCallId: `sub_attach_${msg.internal_id}`,
          toolName: 'attach_to_response',
          args: { attachment_ids: allAttachmentIds },
          argsText: JSON.stringify({ attachment_ids: allAttachmentIds }),
          result: JSON.stringify({
            status: 'attachments_queued',
            count: allAttachments.length,
            attachments: allAttachments,
          }),
          attachments: allAttachments,
          artifact: { attachments: allAttachments },
        });
      }

      if (content.length === 0) {
        content.push({ type: 'text', text: '' });
      }

      processed.push({
        id: `sub_msg_${msg.internal_id}`,
        role: 'assistant',
        turnId: msg.turn_id ?? undefined,
        content,
        createdAt: new Date(msg.timestamp),
        status: { type: 'complete' },
        reasoning_info: msg.reasoning_info ?? undefined,
        internalId: Number(msg.internal_id),
        hasOwnAttachments: (msg.attachments?.length ?? 0) > 0,
        processing_profile_id: msg.processing_profile_id ?? undefined,
      });
      continue;
    }

    // User (or system) row
    const text = extractText(msg.content);
    const userAttachments: NonNullable<Message['attachments']> = [];
    if (Array.isArray(msg.attachments)) {
      for (const att of msg.attachments) {
        const normalized = normalizeAttachment(att);
        const mimeType = String(normalized.mime_type || '');
        const attType: 'image' | 'document' | 'file' = mimeType.startsWith('image/')
          ? 'image'
          : mimeType === 'application/pdf'
            ? 'document'
            : 'file';
        userAttachments.push({
          id: normalized.attachment_id || `att_${userAttachments.length}`,
          type: attType,
          name: normalized.name || 'Attachment',
          content: normalized.content_url,
        });
      }
    }

    processed.push({
      id: `sub_msg_${msg.internal_id}`,
      role: msg.role === 'system' ? 'system' : 'user',
      turnId: msg.turn_id ?? undefined,
      content: [{ type: 'text', text }],
      createdAt: new Date(msg.timestamp),
      internalId: Number(msg.internal_id),
      attachments: userAttachments.length > 0 ? userAttachments : undefined,
    });
  }

  // Merge consecutive tool-only assistant iterations within the same turn so an
  // agentic tool loop reads as a single turn bubble, matching the live thread.
  const merged: Message[] = [];
  for (const message of processed) {
    const previous = merged[merged.length - 1];
    if (
      previous &&
      isToolOnlyAssistantRow(previous) &&
      isToolOnlyAssistantRow(message) &&
      sameTurn(previous, message)
    ) {
      merged[merged.length - 1] = {
        ...previous,
        content: [...previous.content, ...message.content],
      };
      continue;
    }
    merged.push(message);
  }
  return merged;
}

/** Build the live streaming assistant `Message` bubble for an active subconversation turn. */
export function buildLiveTurnMessage(turn: LiveSubconversationTurn): Message {
  const toolParts: MessageContent[] = turn.toolCalls.map((call) => ({
    type: 'tool-call',
    toolCallId: call.id,
    toolName: call.name,
    args: call.args,
    argsText: call.argsText,
    result: call.result,
    outcome: call.outcome,
    awaitingResult: call.result === undefined,
    display: call.display,
    attachments: call.attachments,
    artifact: call.attachments?.length ? { attachments: call.attachments } : undefined,
    textOffset: call.textOffset,
  }));

  if (turn.attachments.length > 0) {
    const normalized = turn.attachments.map(normalizeAttachment);
    const ids = normalized
      .map((att) => att.attachment_id)
      .filter((id): id is string => typeof id === 'string');
    toolParts.push({
      type: 'tool-call',
      toolCallId: `live_attach_${turn.turnId}`,
      toolName: 'attach_to_response',
      args: { attachment_ids: ids },
      argsText: JSON.stringify({ attachment_ids: ids }),
      result: JSON.stringify({
        status: 'attachments_queued',
        count: normalized.length,
        attachments: normalized,
      }),
      attachments: normalized,
      artifact: { attachments: normalized },
      textOffset: turn.text.length,
    });
  }

  const displayText = turn.error
    ? turn.text
      ? `${turn.text}\n\n${turn.error}`
      : turn.error
    : turn.text;

  const interleaved = interleaveTextAndToolCalls(displayText, toolParts);
  const isEmptyLoading = interleaved.length === 0;

  return {
    id: `sub_stream_${turn.turnId}`,
    role: 'assistant',
    turnId: turn.turnId,
    content: isEmptyLoading ? [{ type: 'text', text: LOADING_MARKER }] : interleaved,
    createdAt: turn.startedAt,
    isLoading: isEmptyLoading,
    status: { type: 'running' },
    processing_profile_id: turn.profileId,
  };
}

/** Convert an internal `Message` to the shape `@assistant-ui/react`'s external store expects. */
export function convertSubconversationMessage(message: Message) {
  let content = Array.isArray(message.content)
    ? message.content
    : message.content
      ? [message.content]
      : [{ type: 'text' as const, text: '' }];
  content = content.filter((item) => item && typeof item === 'object');
  if (content.length === 0) {
    content = [{ type: 'text' as const, text: '' }];
  }

  const convertedAttachments = message.attachments?.map((att) => {
    if (typeof att.content === 'string' && att.content) {
      const url = att.content;
      const contentParts =
        att.type === 'image'
          ? [{ type: 'image' as const, image: url }]
          : [{ type: 'data' as const, name: 'url', data: url }];
      return { ...att, content: contentParts };
    }
    return att;
  });

  const { attachments: _attachments, status, ...rest } = message;
  const converted = {
    ...rest,
    ...(message.role === 'assistant' && status ? { status } : {}),
    content,
    metadata: {
      custom: {
        processing_profile_id: message.processing_profile_id,
        reasoning_info: message.reasoning_info,
        internal_id: message.internalId,
        has_own_attachments: message.hasOwnAttachments,
        turn_id: message.turnId,
        unrateable: true,
      },
    },
  };
  if (message.role === 'user' && convertedAttachments && convertedAttachments.length > 0) {
    return { ...converted, attachments: convertedAttachments };
  }
  return converted;
}
