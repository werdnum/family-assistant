import { ArrowLeft, LockKeyhole } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import type { BackendAttachment, BackendConversationMessage } from './types';
import { MarkdownText } from './MarkdownText';

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

const SharedMessage: React.FC<{ message: BackendConversationMessage }> = ({ message }) => {
  const isUser = message.role === 'user';
  const text = messageText(message);
  if (!text && !message.attachments?.length && !message.tool_calls?.length) {
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
        {message.tool_calls?.map((toolCall) => (
          <details key={toolCall.id} className="mt-2 text-sm">
            <summary>Tool: {toolCall.function?.name || toolCall.name || 'unknown'}</summary>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs">
              {String(toolCall.function?.arguments || toolCall.arguments || '')}
            </pre>
          </details>
        ))}
        <SharedAttachments attachments={message.attachments} />
      </Card>
    </div>
  );
};

const SharedConversationPage: React.FC = () => {
  const { token } = useParams<{ token: string }>();
  const [messages, setMessages] = useState<BackendConversationMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    if (!token) {
      setNotFound(true);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    void fetch(`/api/v1/shared-conversations/${token}/messages`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then((data: { messages: BackendConversationMessage[] }) => setMessages(data.messages))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          setNotFound(true);
        }
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [token]);

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
        {loading && <p className="text-center text-muted-foreground">Loading conversation…</p>}
        {notFound && (
          <Card className="p-6 text-center">
            <h2 className="font-semibold">This shared conversation is unavailable</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              The link may be invalid, replaced, or no longer shared.
            </p>
          </Card>
        )}
        {!loading &&
          !notFound &&
          messages.map((message) => <SharedMessage key={message.internal_id} message={message} />)}
      </main>
    </div>
  );
};

export default SharedConversationPage;
