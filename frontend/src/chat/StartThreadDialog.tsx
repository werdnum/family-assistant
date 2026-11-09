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
import { type CarryCandidate, type ContinuePreview, fetchContinuePreview } from './threadsApi';

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

type PreviewState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; preview: ContinuePreview }
  | { status: 'failed'; message: string };

const turnTimeFormat = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  hour: 'numeric',
  minute: '2-digit',
});

/** The Home turns a continuation would bring along, each with a tick to keep or drop it. */
const CarryPreviewList: React.FC<{
  turns: CarryCandidate[];
  selected: ReadonlySet<string>;
  onToggle: (turnId: string, carried: boolean) => void;
  disabled: boolean;
}> = ({ turns, selected, onToggle, disabled }) => (
  <ul
    className="max-h-60 divide-y divide-border overflow-y-auto rounded-md border border-border"
    data-testid="start-thread-carry-preview"
  >
    {turns.map((turn) => {
      const notes = [
        turnTimeFormat.format(new Date(turn.started_at)),
        turn.is_anchor ? 'the message you’re continuing' : null,
        turn.is_echo ? 'a thread’s outcome' : null,
      ].filter(Boolean);
      return (
        <li key={turn.turn_id}>
          <label className="flex items-start gap-2 px-3 py-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 shrink-0"
              checked={turn.is_anchor || selected.has(turn.turn_id)}
              onChange={(event) => onToggle(turn.turn_id, event.target.checked)}
              disabled={disabled || turn.is_anchor}
              data-testid={`carry-turn-${turn.turn_id}`}
            />
            <span className="min-w-0">
              {turn.user_text && (
                <span className="line-clamp-2 block font-medium">You: {turn.user_text}</span>
              )}
              <span className="line-clamp-2 block text-muted-foreground">
                {turn.reply_text ?? '(no reply)'}
              </span>
              <span className="block text-[11px] text-muted-foreground/80">
                {notes.join(' · ')}
              </span>
            </span>
          </label>
        </li>
      );
    })}
  </ul>
);

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
  // Null runs the thread at its profile's default tier. Reset with the
  // profile, since each profile offers its own tiers.
  const [tierId, setTierId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState('');
  const [withContext, setWithContext] = useState(true);
  const [preview, setPreview] = useState<PreviewState>({ status: 'idle' });
  const [carried, setCarried] = useState<ReadonlySet<string>>(new Set());
  // The start last submitted and its id. Resubmitting the same start keeps the
  // id, so a retry after a lost response returns the thread that start made
  // rather than a second; a changed start is a new one, with a new id.
  const [lastStart, setLastStart] = useState<{
    key: string;
    turnId: string;
  } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setProfileId('');
      setTierId(null);
      setPrompt('');
      setWithContext(true);
      setPreview({ status: 'idle' });
      setPreviewWanted(false);
      setCarried(new Set());
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
  const tiers = profile?.model_tiers ?? [];
  const defaultTierId = profile?.default_model_tier ?? null;
  // A tier rides on the opening turn, so a start without a message has none to give.
  const modelTier = trimmedPrompt !== '' ? tierId : null;
  const previewLoading = carriesContext && preview.status === 'loading';
  const canSubmit =
    Boolean(selectedId) &&
    (carriesContext || trimmedPrompt !== '') &&
    !submitting &&
    !previewLoading;

  // Fetched the first time the conversation would come along, then kept for
  // as long as the dialog is open: the ticks are the user's from then on.
  const [previewWanted, setPreviewWanted] = useState(false);
  if (open && carriesContext && !previewWanted) {
    setPreviewWanted(true);
  }
  useEffect(() => {
    if (!open || !previewWanted || !anchor) {
      return;
    }
    const controller = new AbortController();
    setPreview({ status: 'loading' });
    void (async () => {
      try {
        const loaded = await fetchContinuePreview(
          await anchor.resolveMessageId(),
          controller.signal
        );
        setCarried(new Set(loaded.turns.filter((turn) => turn.suggested).map((t) => t.turn_id)));
        setPreview({ status: 'ready', preview: loaded });
      } catch (previewError) {
        if (controller.signal.aborted) {
          return;
        }
        setPreview({
          status: 'failed',
          message: previewError instanceof Error ? previewError.message : String(previewError),
        });
      }
    })();
    return () => controller.abort();
  }, [open, previewWanted, anchor]);

  const toggleCarried = (turnId: string, keep: boolean) =>
    setCarried((current) => {
      const next = new Set(current);
      if (keep) {
        next.add(turnId);
      } else {
        next.delete(turnId);
      }
      return next;
    });
  const carriedTurnIds =
    carriesContext && preview.status === 'ready'
      ? preview.preview.turns
          .filter((turn) => turn.is_anchor || carried.has(turn.turn_id))
          .map((turn) => turn.turn_id)
      : undefined;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    const key = JSON.stringify([
      selectedId,
      trimmedPrompt,
      carriesContext,
      carriedTurnIds,
      modelTier,
    ]);
    const turnId = lastStart?.key === key ? lastStart.turnId : generateUUID();
    setLastStart({ key, turnId });
    setSubmitting(true);
    setError(null);
    try {
      await startThread({
        profile_id: selectedId,
        turn_id: turnId,
        ...(trimmedPrompt ? { prompt: trimmedPrompt } : {}),
        ...(modelTier ? { model_tier: modelTier } : {}),
        ...(anchor
          ? {
              anchor_message_id: await anchor.resolveMessageId(),
              include_context: carriesContext,
              ...(carriedTurnIds ? { carried_turn_ids: carriedTurnIds } : {}),
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
              onChange={(event) => {
                setProfileId(event.target.value);
                setTierId(null);
              }}
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
          {tiers.length > 1 && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="start-thread-tier">Intelligence</Label>
              <NativeSelect
                id="start-thread-tier"
                value={tierId ?? defaultTierId ?? ''}
                onChange={(event) =>
                  setTierId(event.target.value === defaultTierId ? null : event.target.value)
                }
                disabled={submitting}
                data-testid="start-thread-tier"
              >
                {tiers.map((tier) => (
                  <option key={tier.id} value={tier.id}>
                    {tier.label}
                    {tier.id === defaultTierId ? ' (default)' : ''}
                  </option>
                ))}
              </NativeSelect>
              {trimmedPrompt === '' && carriesContext && (
                <p className="text-xs text-muted-foreground" data-testid="start-thread-tier-hint">
                  Applies with an opening message. Without one, choose it in the thread.
                </p>
              )}
            </div>
          )}
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
                  <span
                    className="block text-xs text-muted-foreground"
                    data-testid="start-thread-carry-hint"
                  >
                    {carryHint(preview, withContext)}
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
          {carriesContext && preview.status === 'ready' && (
            <CarryPreviewList
              turns={preview.preview.turns}
              selected={carried}
              onToggle={toggleCarried}
              disabled={submitting}
            />
          )}
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

function carryHint(preview: PreviewState, withContext: boolean): string {
  if (!withContext) {
    return 'Only the message you add goes to the thread.';
  }
  switch (preview.status) {
    case 'ready':
      return preview.preview.suggested_by === 'assistant'
        ? 'Ticked messages are copied into the thread. The ones that look related are ticked for you.'
        : 'Ticked messages are copied into the thread.';
    case 'failed':
      return `${preview.message} This message and up to four before it will be copied.`;
    default:
      return 'Checking what comes along…';
  }
}
