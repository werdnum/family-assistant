import { ChevronRightIcon, CornerDownLeftIcon } from 'lucide-react';
import React from 'react';
import { ThreadStateDot } from './ThreadStateDot';
import { useThreadsContext } from './threadsContext';

/**
 * The attribution on a Home message that echoes a thread's outcome: which
 * thread it came from, opening that thread on click.
 */
export const ThreadEchoHeader: React.FC<{ threadConversationId: string }> = ({
  threadConversationId,
}) => {
  const threads = useThreadsContext();
  const thread = threads?.threadsByConversationId.get(threadConversationId);
  const label = thread ? (
    <>
      From thread: <span className="font-medium text-foreground/90">{thread.title}</span>
    </>
  ) : (
    'From a thread'
  );
  const className =
    'mb-1 flex max-w-full min-w-0 items-center gap-1.5 text-xs text-muted-foreground';
  if (!threads) {
    return (
      <div className={className} data-testid="thread-echo-header">
        <CornerDownLeftIcon size={12} className="shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate">{label}</span>
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={() => threads.openConversation(threadConversationId)}
      className={`${className} rounded-md text-left hover:text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring`}
      aria-label={thread ? `Open thread: ${thread.title}` : 'Open the thread this came from'}
      data-testid="thread-echo-header"
      data-conversation-id={threadConversationId}
    >
      <CornerDownLeftIcon size={12} className="shrink-0" aria-hidden="true" />
      {thread && <ThreadStateDot state={thread.state} />}
      <span className="min-w-0 truncate">{label}</span>
      <ChevronRightIcon size={12} className="shrink-0" aria-hidden="true" />
    </button>
  );
};
