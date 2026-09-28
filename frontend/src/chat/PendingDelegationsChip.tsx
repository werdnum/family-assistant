import { Loader2Icon } from 'lucide-react';
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
}

/** One line per background delegation the conversation is waiting on. */
export const PendingDelegationsChip: React.FC<PendingDelegationsChipProps> = ({ delegations }) => {
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
      {delegations.map((delegation) => (
        <li
          key={delegation.delegation_id}
          className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full border border-border/60 bg-muted/50 px-3 py-1 text-xs text-muted-foreground"
          title={delegation.request_preview}
          data-testid="pending-delegation"
        >
          <Loader2Icon className="h-3 w-3 flex-shrink-0 animate-spin" aria-hidden="true" />
          <span className="truncate">
            <span className="font-medium text-foreground">
              {getProfileDisplayName(delegation.target_profile_id)}
            </span>
            {' · '}
            {delegationStatusLabel(delegation)}
            {' · '}
            {formatStartedAgo(delegation.started_at ?? delegation.created_at, now)}
          </span>
        </li>
      ))}
    </ul>
  );
};
