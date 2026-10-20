import { ArrowLeftIcon, CheckIcon, CornerDownRightIcon, Loader2Icon } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { getProfileDisplayName } from './profileNames';
import { ThreadStateDot, threadStatusText } from './ThreadStateDot';
import type { ThreadSummary } from './types';

interface ThreadBannerProps {
  thread: ThreadSummary;
  /**
   * The Home message a continued thread grew from, when Home has it loaded.
   * The anchor is display-only and may since have moved, so it can be missing.
   */
  anchorQuote?: string | null;
  onOpenHome: (homeConversationId: string) => void;
  onMarkDone: (conversationId: string) => void;
  /** A Mark done for this thread in flight, or the last one having failed. */
  markDoneStatus: 'pending' | 'failed' | null;
  /**
   * In the right-hand panel, Home is beside the thread and the panel's header
   * names it, so the banner leaves out the way back and the title.
   */
  compact?: boolean;
}

/**
 * Where an open thread came from, the way back to Home, where it stands, and
 * Mark done for a thread that is waiting on the user or has failed.
 */
export const ThreadBanner: React.FC<ThreadBannerProps> = ({
  thread,
  anchorQuote = null,
  onOpenHome,
  onMarkDone,
  markDoneStatus,
  compact = false,
}) => {
  // Work in progress outranks a close, so a working thread would stay working;
  // it can be marked done once it stops.
  const canMarkDone = thread.state === 'waiting' || thread.state === 'failed';
  return (
    <div
      className="flex-shrink-0 border-b border-border/50 bg-muted/20 px-4 py-2 md:px-6"
      data-testid="thread-banner"
      data-thread-state={thread.state}
    >
      <div className="mx-auto flex max-w-3xl flex-col gap-1.5">
        <div className="flex min-w-0 items-center gap-2">
          {!compact && (
            <>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 shrink-0 gap-1 px-2"
                onClick={() => onOpenHome(thread.home_conversation_id)}
                data-testid="thread-breadcrumb-home"
              >
                <ArrowLeftIcon size={14} aria-hidden="true" />
                Home
              </Button>
              <span className="text-muted-foreground" aria-hidden="true">
                /
              </span>
              <ThreadStateDot state={thread.state} />
              <span
                className="min-w-0 truncate text-sm font-medium"
                data-testid="thread-banner-title"
              >
                {thread.title}
              </span>
            </>
          )}
          {/* Narrow screens keep the row to the way back, the title and the action. */}
          <Badge
            variant="secondary"
            className="hidden shrink-0 text-[10px] font-normal sm:inline-flex"
          >
            {getProfileDisplayName(thread.profile_id)}
          </Badge>
          {thread.state === 'working' ? (
            <span
              className="ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
              data-testid="thread-working-indicator"
            >
              <Loader2Icon size={12} className="animate-spin" aria-hidden="true" />
              Working…
            </span>
          ) : (
            <span
              className={cn(
                'ml-auto shrink-0 text-xs',
                thread.state === 'failed'
                  ? 'font-medium text-destructive'
                  : 'text-muted-foreground',
                // The dot already says where it stands; Mark done needs the room.
                canMarkDone && 'hidden sm:inline'
              )}
              data-testid="thread-banner-state"
            >
              {threadStatusText(thread)}
            </span>
          )}
          {canMarkDone && (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto h-7 shrink-0 gap-1 px-2 text-xs sm:ml-0"
              onClick={() => onMarkDone(thread.conversation_id)}
              disabled={markDoneStatus === 'pending'}
              data-testid="thread-mark-done"
            >
              {markDoneStatus === 'pending' ? (
                <Loader2Icon size={12} className="animate-spin" aria-hidden="true" />
              ) : (
                <CheckIcon size={12} aria-hidden="true" />
              )}
              Mark done
            </Button>
          )}
        </div>
        {markDoneStatus === 'failed' && (
          <div
            role="alert"
            className="text-xs text-destructive"
            data-testid="thread-mark-done-failed"
          >
            Couldn't mark this thread done. Try again.
          </div>
        )}
        <div
          className="flex min-w-0 items-start gap-1.5 text-xs text-muted-foreground"
          data-testid="thread-origin"
        >
          <CornerDownRightIcon size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 line-clamp-2 break-words">
            Started from Home
            {anchorQuote && (
              <>
                {': '}
                <span className="text-foreground/80" data-testid="thread-anchor-quote">
                  “{anchorQuote}”
                </span>
              </>
            )}
            {thread.seed_request && (
              <>
                {' · Assistant asked: '}
                <span className="text-foreground/80">“{thread.seed_request}”</span>
              </>
            )}
          </span>
        </div>
      </div>
    </div>
  );
};
