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
import { Label } from '@/components/ui/label';
import { NativeSelect } from '@/components/ui/native-select';
import { Textarea } from '@/components/ui/textarea';
import { generateUUID } from '../utils/uuid';
import { useChatShell } from './chatShell';
import { getProfileDisplayName } from './profileNames';
import { type ServiceProfile, useProfiles } from './profilesContext';

/** The Home message a thread is continued from. */
export interface ContinueAnchor {
  /** The message's text, as the dialog quotes it. */
  quote: string;
  /** The message's stored id, looked up if the message was streamed here. */
  resolveMessageId: () => Promise<number>;
}

interface StartThreadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Continue from this Home message; a new thread when absent. */
  anchor?: ContinueAnchor | null;
}

/** The profiles a user can start a thread on. */
export function threadProfiles(profiles: ServiceProfile[]): ServiceProfile[] {
  return profiles.filter(
    (profile) => profile.user_selectable !== false && !profile.delegation_only
  );
}

/**
 * Start a thread on a profile the user picks: "New thread", or "Continue in a
 * thread" from a Home message. Continuing brings the exchange along only into a
 * profile that receives household context; anywhere else the thread starts from
 * the user's message alone, and the dialog says so.
 */
export const StartThreadDialog: React.FC<StartThreadDialogProps> = ({
  open,
  onOpenChange,
  anchor = null,
}) => {
  const { startThread } = useChatShell();
  const { profiles, profilesById, defaultProfileId } = useProfiles();
  const choices = threadProfiles(profiles);
  const [profileId, setProfileId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [withContext, setWithContext] = useState(true);
  // The start last submitted and its id. Resubmitting the same start keeps the
  // id, so a retry after a lost response returns the thread that start made
  // rather than a second; a changed start is a new one, with a new id.
  const [lastStart, setLastStart] = useState<{ key: string; turnId: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setProfileId('');
      setPrompt('');
      setWithContext(true);
      setLastStart(null);
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const selectedId = profileId || defaultProfileId || choices[0]?.id || '';
  const profile = profilesById[selectedId];
  const profileName = getProfileDisplayName(selectedId);
  const receivesContext = profile?.receives_household_context === true;
  const carriesContext = anchor !== null && receivesContext && withContext;
  const trimmedPrompt = prompt.trim();
  const canSubmit = Boolean(selectedId) && (carriesContext || trimmedPrompt !== '') && !submitting;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    const key = JSON.stringify([selectedId, trimmedPrompt, carriesContext]);
    const turnId = lastStart?.key === key ? lastStart.turnId : generateUUID();
    setLastStart({ key, turnId });
    setSubmitting(true);
    setError(null);
    try {
      await startThread({
        profile_id: selectedId,
        turn_id: turnId,
        ...(trimmedPrompt ? { prompt: trimmedPrompt } : {}),
        ...(anchor
          ? {
              anchor_message_id: await anchor.resolveMessageId(),
              include_context: carriesContext,
            }
          : {}),
      });
      onOpenChange(false);
    } catch (startError) {
      setError(
        startError instanceof Error ? startError.message : "Couldn't start the thread. Try again."
      );
      setSubmitting(false);
    }
  };

  const title = anchor ? 'Continue in a thread' : 'New thread';
  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent className="sm:max-w-lg" data-testid="start-thread-dialog">
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              {anchor
                ? 'Carry on with this in a thread of its own, on the profile you choose.'
                : 'A thread runs on its own profile, beside Home, until the job is done.'}
            </DialogDescription>
          </DialogHeader>
          {anchor && (
            <blockquote
              className="line-clamp-3 border-l-2 border-border pl-3 text-sm text-muted-foreground"
              data-testid="start-thread-anchor"
            >
              {anchor.quote}
            </blockquote>
          )}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="start-thread-profile">Profile</Label>
            <NativeSelect
              id="start-thread-profile"
              value={selectedId}
              onChange={(event) => setProfileId(event.target.value)}
              disabled={submitting}
              data-testid="start-thread-profile"
            >
              {choices.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {getProfileDisplayName(choice.id)}
                  {anchor && !choice.receives_household_context ? ' (your message only)' : ''}
                </option>
              ))}
            </NativeSelect>
          </div>
          {anchor &&
            (receivesContext ? (
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4"
                  checked={withContext}
                  onChange={(event) => setWithContext(event.target.checked)}
                  disabled={submitting}
                  data-testid="start-thread-with-context"
                />
                <span>
                  Bring the conversation along
                  <span className="block text-xs text-muted-foreground">
                    This exchange and up to four before it are copied into the thread.
                  </span>
                </span>
              </label>
            ) : (
              <p
                className="rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground"
                data-testid="start-thread-request-only"
              >
                {profileName} doesn't receive household context, so Home's conversation can't go
                with it. The thread starts from your message alone and links back here.
              </p>
            ))}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="start-thread-prompt">
              {carriesContext ? 'Message (optional)' : 'Message'}
            </Label>
            <Textarea
              id="start-thread-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder={
                carriesContext ? 'Anything to add before it picks this up?' : 'What should it do?'
              }
              rows={3}
              disabled={submitting}
              data-testid="start-thread-prompt"
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive" data-testid="start-thread-error">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={!canSubmit} data-testid="start-thread-submit">
              {submitting && <Loader2Icon size={14} className="mr-1 animate-spin" />}
              Start thread
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
};
