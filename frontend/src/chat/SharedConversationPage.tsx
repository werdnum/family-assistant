import { ArrowLeft, LockKeyhole } from 'lucide-react';
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import type { BackendAttachment, BackendConversationMessage, BackendToolCall } from './types';
import { MarkdownText } from './MarkdownText';
import { ToolGroupShell } from './ToolGroupShell';
import { getToolIconInfo } from './toolIconMapping';

function messageText(message: BackendConversationMessage): string {
  if (typeof message.content === 'string') {
    return message.content;
  }
  if (Array.isArray(message.content)) {
    return message.content
      .filter((part): part is { type: 'text'; text: string } => part.type === 'text')
      .map((part) => part.text)
      .join('\n\n');
  }
  return '';
}

interface SharedToolCall {
  id: string;
  name: string;
  argsText: string;
  resultText: string;
}

interface ToolResult {
  text: string;
  attachments: BackendAttachment[];
}

function toolCallName(toolCall: BackendToolCall): string {
  return toolCall.function?.name || toolCall.name || 'unknown';
}

function toolCallArgsText(toolCall: BackendToolCall): string {
  const args = toolCall.function?.arguments ?? toolCall.arguments;
  if (args === undefined || args === null) {
    return '';
  }
  return typeof args === 'string' ? args : JSON.stringify(args, null, 2);
}

/**
 * Index every tool result by the call it answers, so results can be folded into
 * the collapsed group for that call rather than dumped into the transcript.
 */
function collectToolResults(messages: BackendConversationMessage[]): Map<string, ToolResult> {
  const results = new Map<string, ToolResult>();
  for (const message of messages) {
    if (message.role === 'tool' && message.tool_call_id) {
      results.set(message.tool_call_id, {
        text: messageText(message),
        attachments: message.attachments ?? [],
      });
    }
  }
  return results;
}

function isToolOnlyAssistantMessage(message: BackendConversationMessage): boolean {
  return (
    message.role === 'assistant' &&
    (message.tool_calls?.length ?? 0) > 0 &&
    messageText(message).trim() === ''
  );
}

/**
 * Two rows belong to the same turn unless both name a turn and the names
 * differ. History predating turn ids leaves them unset, and treating that as a
 * boundary would stop merging entirely on older transcripts.
 */
function sameTurn(a: BackendConversationMessage, b: BackendConversationMessage): boolean {
  return !a.turn_id || !b.turn_id || a.turn_id === b.turn_id;
}

/**
 * An agentic turn persists one assistant row per LLM iteration. Merge the
 * tool-only ones into a single group, as the live thread and the iOS shared
 * view do, so one turn reads as one box. Assistant text ends a run, and so
 * does a turn boundary — the rows that would otherwise separate two turns
 * (subconversation and internal trigger rows) are not sent to this client.
 */
function mergeToolOnlyAssistantMessages(
  messages: BackendConversationMessage[]
): BackendConversationMessage[] {
  const merged: BackendConversationMessage[] = [];
  for (const message of messages) {
    const previous = merged[merged.length - 1];
    if (
      previous &&
      isToolOnlyAssistantMessage(previous) &&
      isToolOnlyAssistantMessage(message) &&
      sameTurn(previous, message)
    ) {
      merged[merged.length - 1] = {
        ...previous,
        tool_calls: [...(previous.tool_calls ?? []), ...(message.tool_calls ?? [])],
        attachments: [...(previous.attachments ?? []), ...(message.attachments ?? [])],
      };
      continue;
    }
    merged.push(message);
  }
  return merged;
}

function buildSharedToolCalls(
  message: BackendConversationMessage,
  toolResults: Map<string, ToolResult>
): SharedToolCall[] {
  return (message.tool_calls ?? []).map((toolCall) => ({
    id: toolCall.id,
    name: toolCallName(toolCall),
    argsText: toolCallArgsText(toolCall),
    resultText: toolResults.get(toolCall.id)?.text ?? '',
  }));
}

const SharedToolGroup: React.FC<{ toolCalls: SharedToolCall[] }> = ({ toolCalls }) => {
  const [isExpanded, setIsExpanded] = useState(false);

  return (
    <ToolGroupShell
      toolNames={toolCalls.map((toolCall) => toolCall.name)}
      toolCount={toolCalls.length}
      isExpanded={isExpanded}
      onOpenChange={setIsExpanded}
    >
      {toolCalls.map((toolCall) => {
        const { icon: Icon } = getToolIconInfo(toolCall.name);
        return (
          <div key={toolCall.id} className="rounded-md border border-border/50 p-2">
            <div className="flex items-center gap-2 text-xs font-medium">
              <Icon className="h-3.5 w-3.5" />
              {toolCall.name}
            </div>
            {toolCall.argsText && (
              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-muted-foreground">
                {toolCall.argsText}
              </pre>
            )}
            {toolCall.resultText && (
              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs">
                {toolCall.resultText}
              </pre>
            )}
          </div>
        );
      })}
    </ToolGroupShell>
  );
};

function isConversationResponse(
  value: unknown
): value is { messages: BackendConversationMessage[] } {
  return (
    typeof value === 'object' &&
    value !== null &&
    'messages' in value &&
    Array.isArray(value.messages)
  );
}

const SharedAttachments: React.FC<{ attachments?: BackendAttachment[] }> = ({ attachments }) => {
  if (!attachments?.length) {
    return null;
  }
  return (
    <div className="mt-3 flex flex-wrap gap-3">
      {attachments.map((attachment) => {
        const url = attachment.content_url;
        if (!url || !attachment.attachment_id) {
          return null;
        }
        const name = attachment.name || String(attachment.description || 'Attachment');
        const mimeType = String(attachment.mime_type || '');
        return mimeType.startsWith('image/') ? (
          <a key={attachment.attachment_id} href={url} target="_blank" rel="noreferrer">
            <img src={url} alt={name} className="max-h-80 rounded-lg border object-contain" />
          </a>
        ) : (
          <a
            key={attachment.attachment_id}
            href={url}
            className="text-sm text-primary underline underline-offset-4"
          >
            {name}
          </a>
        );
      })}
    </div>
  );
};

const SharedMessage: React.FC<{
  message: BackendConversationMessage;
  toolResults: Map<string, ToolResult>;
}> = ({ message, toolResults }) => {
  const isUser = message.role === 'user';
  // A tool result only reaches here when no tool call in the transcript claimed
  // it; show it collapsed rather than dropping it or dumping it as prose.
  const isOrphanToolResult = message.role === 'tool';
  const orphanResultText = isOrphanToolResult ? messageText(message) : '';
  const text = isOrphanToolResult ? '' : messageText(message);
  // An orphan with no output at all has nothing to collapse, so synthesizing a
  // call for it would render an empty group instead of skipping the message.
  const toolCalls = isOrphanToolResult
    ? orphanResultText
      ? [{ id: message.internal_id, name: 'unknown', argsText: '', resultText: orphanResultText }]
      : []
    : buildSharedToolCalls(message, toolResults);
  // Attachments produced by this message's tools belong with the response, not
  // inside the collapsed group — they are usually the point of the answer.
  const attachments = [
    ...(message.attachments ?? []),
    ...toolCalls.flatMap((toolCall) => toolResults.get(toolCall.id)?.attachments ?? []),
  ];
  if (!text && !attachments.length && !toolCalls.length) {
    return null;
  }
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <Card
        className={`max-w-[85%] px-4 py-3 ${
          isUser ? 'bg-primary text-primary-foreground' : 'bg-card'
        }`}
      >
        <div className="mb-2 text-xs font-medium opacity-70">
          {isUser
            ? 'Conversation owner'
            : message.role === 'assistant'
              ? 'Assistant'
              : message.role}
        </div>
        {text && <MarkdownText text={text} />}
        {toolCalls.length > 0 && <SharedToolGroup toolCalls={toolCalls} />}
        <SharedAttachments attachments={attachments} />
      </Card>
    </div>
  );
};

const SharedConversationPage: React.FC = () => {
  const { token } = useParams<{ token: string }>();
  const [messages, setMessages] = useState<BackendConversationMessage[]>([]);
  const [loadState, setLoadState] = useState<
    'loading' | 'ready' | 'not-found' | 'authentication-required' | 'error'
  >('loading');
  const [requestVersion, setRequestVersion] = useState(0);

  const toolResults = useMemo(() => collectToolResults(messages), [messages]);
  const visibleMessages = useMemo(() => {
    const claimedResultIds = new Set(
      messages.flatMap((message) => (message.tool_calls ?? []).map((toolCall) => toolCall.id))
    );
    // Merge after filtering: the tool results between two tool-only assistant
    // rows are gone by then, so the rows are genuinely adjacent.
    return mergeToolOnlyAssistantMessages(
      messages.filter(
        (message) =>
          message.role !== 'tool' ||
          !message.tool_call_id ||
          !claimedResultIds.has(message.tool_call_id)
      )
    );
  }, [messages]);

  useEffect(() => {
    setLoadState('loading');
    if (!token) {
      setLoadState('not-found');
      return;
    }
    const controller = new AbortController();
    void fetch(`/api/v1/shared-conversations/${token}/messages`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 404) {
          setLoadState('not-found');
          return null;
        }
        if (response.status === 401 || response.status === 403) {
          setLoadState('authentication-required');
          return null;
        }
        if (!response.ok) {
          throw new Error(`Shared conversation request failed with status ${response.status}`);
        }
        return response.json();
      })
      .then((data: unknown) => {
        if (data === null) {
          return;
        }
        if (!isConversationResponse(data)) {
          throw new Error('Shared conversation response is malformed');
        }
        setMessages(data.messages);
        setLoadState('ready');
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          console.error('Failed to load shared conversation:', error);
          setLoadState('error');
        }
      });
    return () => controller.abort();
  }, [token, requestVersion]);

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-4xl items-center gap-3 px-4 py-3">
          <Button asChild variant="ghost" size="sm">
            <Link to="/chat" aria-label="Back to chat">
              <ArrowLeft className="h-4 w-4" />
            </Link>
          </Button>
          <div>
            <h1 className="font-semibold">Shared conversation</h1>
            <p className="flex items-center gap-1 text-xs text-muted-foreground">
              <LockKeyhole className="h-3 w-3" /> Read only
            </p>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-4xl space-y-4 px-4 py-8">
        {loadState === 'loading' && (
          <p className="text-center text-muted-foreground">Loading conversation…</p>
        )}
        {loadState === 'not-found' && (
          <Card className="p-6 text-center">
            <h2 className="font-semibold">This shared conversation is unavailable</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              The link may be invalid, replaced, or no longer shared.
            </p>
          </Card>
        )}
        {loadState === 'authentication-required' && (
          <Card className="p-6 text-center">
            <h2 className="font-semibold">Sign in to view this conversation</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              Shared conversations are available only to authorized Family Assistant users.
            </p>
            <Button asChild className="mt-4">
              <a href={window.location.href}>Sign in again</a>
            </Button>
          </Card>
        )}
        {loadState === 'error' && (
          <Card className="p-6 text-center">
            <h2 className="font-semibold">Could not load the shared conversation</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              Check your connection and try again.
            </p>
            <Button className="mt-4" onClick={() => setRequestVersion((version) => version + 1)}>
              Try again
            </Button>
          </Card>
        )}
        {loadState === 'ready' &&
          visibleMessages.map((message) => (
            <SharedMessage key={message.internal_id} message={message} toolResults={toolResults} />
          ))}
      </main>
    </div>
  );
};

export default SharedConversationPage;
