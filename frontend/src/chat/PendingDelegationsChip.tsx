import { ChevronRightIcon, Loader2Icon } from 'lucide-react';
import React from 'react';
import { getProfileDisplayName } from './profileNames';
import type { PendingDelegation } from './usePendingDelegations';

export const delegationStatusLabel = (delegation: PendingDelegation): string => {
  if (delegation.cancel_requested) {
    return 'Cancelling';
  }
  switch (delegation.status) {
    case 'queued':
      return 'Queued';
    case 'completed':
    case 'failed':
      // The run is done; the reply about it is still being written.
      return 'Finishing up';
    default:
      if (delegation.children_total > 0) {
        return `${delegation.children_finished} of ${delegation.children_total} done`;
      }
      return 'Working on it';
  }
};

export const formatStartedAgo = (iso: string, now: number): string => {
  const minutes = Math.floor((now - new Date(iso).getTime()) / 60000);
  if (minutes < 1) {
    return 'started just now';
  }
  if (minutes < 60) {
    return `started ${minutes} min ago`;
  }
  return `started ${Math.floor(minutes / 60)} h ago`;
};

interface PendingDelegationsChipProps {
  delegations: PendingDelegation[];
  onSelectDelegation?: (delegation: PendingDelegation) => void;
}

/** One line per background delegation the conversation is waiting on. */
export const PendingDelegationsChip: React.FC<PendingDelegationsChipProps> = ({
  delegations,
  onSelectDelegation,
}) => {
  if (delegations.length === 0) {
    return null;
  }
  const now = Date.now();
  return (
    <ul
      className="flex flex-wrap gap-2 max-w-3xl mx-auto w-full"
      aria-label="Background work in progress"
      data-testid="pending-delegations"
    >
      {delegations.map((delegation) => {
        const displayName = getProfileDisplayName(delegation.target_profile_id);
        const canPeek = Boolean(onSelectDelegation && delegation.subconversation_id);
        const content = (
          <>
            <Loader2Icon className="h-3 w-3 flex-shrink-0 animate-spin" aria-hidden="true" />
            <span className="truncate">
              <span className="font-medium text-foreground">{displayName}</span>
              {' · '}
              {delegationStatusLabel(delegation)}
              {' · '}
              {formatStartedAgo(delegation.started_at ?? delegation.created_at, now)}
            </span>
            {canPeek && (
              <ChevronRightIcon
                className="h-3 w-3 flex-shrink-0 text-muted-foreground/70"
                aria-hidden="true"
              />
            )}
          </>
        );

        if (canPeek) {
          return (
            <li key={delegation.delegation_id} className="min-w-0 max-w-full">
              <button
                type="button"
                onClick={() => onSelectDelegation?.(delegation)}
                className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full border border-border/60 bg-muted/50 px-3 py-1 text-xs text-muted-foreground transition-colors hover:border-border hover:bg-muted/80 hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
                title={delegation.request_preview}
                aria-label={`Peek into ${displayName}: ${delegation.request_preview}`}
                data-testid="pending-delegation"
              >
                {content}
              </button>
            </li>
          );
        }

        return (
          <li
            key={delegation.delegation_id}
            className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full border border-border/60 bg-muted/50 px-3 py-1 text-xs text-muted-foreground"
            title={delegation.request_preview}
            data-testid="pending-delegation"
          >
            {content}
          </li>
        );
      })}
    </ul>
  );
};
