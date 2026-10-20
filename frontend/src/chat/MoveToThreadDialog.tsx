import { Loader2Icon } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useChatShell } from './chatShell';
import { getProfileDisplayName } from './profileNames';
import { type ServiceProfile, useProfiles } from './profilesContext';
import { moveTurnToThread } from './threadsApi';
import { ThreadStateDot, threadStatusText } from './ThreadStateDot';
import type { ThreadSummary } from './types';

/**
 * Why a Home turn can't be moved into a thread right now, or null when it can.
 * A thread's profile must receive household context, and the thread must not
 * be working: a turn moved in mid-reply would land inside that reply.
 */
export function moveBlocker(
  thread: ThreadSummary,
  profile: ServiceProfile | undefined,
  profilesLoading: boolean
): string | null {
  if (!profile) {
    return profilesLoading ? 'Checking its profile…' : 'Its profile is no longer available';
  }
  if (profile.receives_household_context !== true) {
    return `${getProfileDisplayName(thread.profile_id)} doesn't receive household context`;
  }
  if (thread.state === 'working') {
    return 'Working: wait for it to finish';
  }
  return null;
}

interface MoveToThreadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The quoted text of the turn being moved. */
  quote: string;
  /** Any stored id of the turn, looked up if the turn was streamed here. */
  resolveMessageId: () => Promise<number>;
  /** The move went through: Home holds a marker in the turn's place. */
  onMoved: (thread: ThreadSummary) => void;
}

/** Move a whole Home turn into one of Home's threads. */
export const MoveToThreadDialog: React.FC<MoveToThreadDialogProps> = ({
  open,
  onOpenChange,
  quote,
  resolveMessageId,
  onMoved,
}) => {
  const { homeThreads, updateHomeThread, refreshHomeThreads } = useChatShell();
  const { profilesById, isLoading: profilesLoading } = useProfiles();
  const [selected, setSelected] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setSelected(null);
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const options = homeThreads.map((thread) => ({
    thread,
    blocker: moveBlocker(thread, profilesById[thread.profile_id], profilesLoading),
  }));
  const anyAvailable = options.some((option) => option.blocker === null);
  const selectedOption = options.find((option) => option.thread.conversation_id === selected);
  const canSubmit = selectedOption !== undefined && selectedOption.blocker === null && !submitting;

  const submit = async () => {
    if (!canSubmit || !selected) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const moved = await moveTurnToThread(selected, await resolveMessageId());
      updateHomeThread(moved.thread.conversation_id, moved.thread);
      refreshHomeThreads();
      onOpenChange(false);
      onMoved(moved.thread);
    } catch (moveError) {
      setError(
        moveError instanceof Error ? moveError.message : "Couldn't move the exchange. Try again."
      );
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent className="sm:max-w-lg" data-testid="move-to-thread-dialog">
        <DialogHeader>
          <DialogTitle>Move to thread</DialogTitle>
          <DialogDescription>
            The whole exchange moves into the thread, and Home keeps a note of where it went.
          </DialogDescription>
        </DialogHeader>
        <blockquote className="line-clamp-3 border-l-2 border-border pl-3 text-sm text-muted-foreground">
          {quote}
        </blockquote>
        {options.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="move-to-thread-none">
            Home has no threads yet. Use Continue in a thread to start one from this exchange.
          </p>
        ) : (
          <div
            role="radiogroup"
            aria-label="Thread to move into"
            className="flex max-h-72 flex-col gap-1 overflow-y-auto"
          >
            {options.map(({ thread, blocker }) => {
              const isSelected = thread.conversation_id === selected;
              return (
                <button
                  key={thread.conversation_id}
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  disabled={blocker !== null || submitting}
                  onClick={() => setSelected(thread.conversation_id)}
                  className={cn(
                    'flex min-w-0 flex-col gap-0.5 rounded-lg border px-3 py-2 text-left text-sm transition-colors',
                    isSelected
                      ? 'border-primary bg-primary/5'
                      : 'border-border/60 hover:bg-muted/50',
                    blocker !== null && 'cursor-not-allowed opacity-60 hover:bg-transparent'
                  )}
                  data-testid={`move-option-${thread.conversation_id}`}
                  data-available={blocker === null}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <ThreadStateDot state={thread.state} />
                    <span className="min-w-0 flex-1 truncate font-medium">{thread.title}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {getProfileDisplayName(thread.profile_id)}
                    </span>
                  </span>
                  <span
                    className="pl-4 text-xs text-muted-foreground"
                    data-testid="move-option-detail"
                  >
                    {blocker ?? threadStatusText(thread)}
                  </span>
                </button>
              );
            })}
          </div>
        )}
        {options.length > 0 && !anyAvailable && (
          <p className="text-xs text-muted-foreground" data-testid="move-to-thread-unavailable">
            None of Home's threads can take this exchange right now.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive" data-testid="move-to-thread-error">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            data-testid="move-to-thread-submit"
          >
            {submitting && <Loader2Icon size={14} className="mr-1 animate-spin" />}
            Move
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
