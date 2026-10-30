import { AlertCircleIcon, CheckCircle2Icon, ChevronRightIcon } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { getProfileDisplayName } from './profileNames';
import { ThreadStateDot, ThreadUnreadBadge, threadStatusText } from './ThreadStateDot';
import { useThreadsContext } from './threadsContext';

interface StartThreadCardProps {
  toolCallId?: string;
  /** Shown instead when no thread is known for this call. */
  fallback: React.ReactNode;
}

/**
 * The live card a `start_thread` call leaves in Home: the thread's state, title,
 * profile, unread count and latest message, opening the thread on click. A
 * finished thread reads as finished: its outcome when the assistant closed it,
 * or that the user did. A call whose thread is not (or not yet) in the threads
 * list, such as one the tool refused, renders as any other tool call.
 */
export const StartThreadCard: React.FC<StartThreadCardProps> = ({ toolCallId, fallback }) => {
  const threads = useThreadsContext();
  const thread = toolCallId ? threads?.threadsBySourceToolCallId.get(toolCallId) : undefined;
  if (!threads || !thread) {
    return <>{fallback}</>;
  }
  const detail = thread.preview || thread.seed_request;
  const status = threadStatusText(thread);
  const isDone = thread.state === 'done';
  const isFailed = thread.state === 'failed';
  const unread = thread.unread_count ?? 0;
  return (
    <button
      type="button"
      onClick={() => threads.openConversation(thread.conversation_id)}
      className={cn(
        'my-1 flex w-full min-w-0 flex-col gap-1 rounded-xl border px-4 py-3 text-left shadow-xs transition-colors hover:border-primary/40 hover:bg-muted/40 focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
        isFailed
          ? 'border-destructive/40 bg-destructive/5'
          : isDone
            ? 'border-border/50 bg-muted/30 shadow-none'
            : 'border-border/70 bg-background'
      )}
      aria-label={`Open thread: ${thread.title} (${status}${unread ? `, ${unread} unread` : ''})`}
      data-testid="thread-card"
      data-thread-state={thread.state}
      data-closed-by={thread.closed_by ?? undefined}
      data-conversation-id={thread.conversation_id}
    >
      <span className="flex min-w-0 items-center gap-2">
        <ThreadStateDot state={thread.state} />
        <span
          className={cn('min-w-0 flex-1 truncate font-medium', isDone && 'text-muted-foreground')}
          data-testid="thread-card-title"
        >
          {thread.title}
        </span>
        <ThreadUnreadBadge count={unread} />
        <Badge
          variant="secondary"
          className="shrink-0 text-[10px] font-normal"
          data-testid="thread-card-profile"
        >
          {getProfileDisplayName(thread.profile_id)}
        </Badge>
        <span className="flex shrink-0 items-center text-xs text-muted-foreground">
          open
          <ChevronRightIcon size={12} aria-hidden="true" />
        </span>
      </span>
      <span
        className={cn(
          'flex min-w-0 gap-1.5 pl-4 text-xs text-muted-foreground',
          isDone ? 'items-start' : 'items-center'
        )}
      >
        <span
          className={cn(
            'flex shrink-0 items-center gap-1',
            isFailed && 'font-medium text-destructive',
            isDone && 'font-medium text-emerald-700 dark:text-emerald-400'
          )}
          data-testid="thread-card-state"
        >
          {isDone && <CheckCircle2Icon size={12} aria-hidden="true" />}
          {isFailed && <AlertCircleIcon size={12} aria-hidden="true" />}
          {status}
        </span>
        {detail && (
          <>
            <span aria-hidden="true">·</span>
            <span
              className={cn(
                'min-w-0',
                isDone && thread.closed_by === 'assistant'
                  ? 'line-clamp-2 break-words text-foreground/80'
                  : 'truncate'
              )}
              data-testid="thread-card-preview"
            >
              {detail}
            </span>
          </>
        )}
      </span>
    </button>
  );
};
