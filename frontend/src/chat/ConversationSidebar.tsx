import { GitBranchPlusIcon, HomeIcon, MessageSquarePlusIcon, SearchIcon } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  THREAD_STATE_LABELS,
  ThreadStateDot,
  ThreadUnreadBadge,
  threadStatusText,
} from './ThreadStateDot';
import { Conversation, ConversationSidebarProps, ThreadState } from './types';

const SEARCH_DEBOUNCE_MS = 300;

// Threads that need the user come first.
const THREAD_GROUPS: ThreadState[] = ['waiting', 'working', 'failed', 'done'];

interface SearchResults {
  query: string;
  conversations: Conversation[];
}

const ConversationSidebar: React.FC<ConversationSidebarProps> = ({
  conversations = [],
  conversationsLoading = false,
  currentConversationId,
  onConversationSelect,
  onNewChat,
  isOpen,
  onRefresh: _onRefresh,
  isMobile = false,
  home = null,
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResults | null>(null);
  const [searchFailed, setSearchFailed] = useState(false);
  const trimmedQuery = searchQuery.trim();

  // Search runs on the server, so it covers everything said in every
  // conversation rather than only the previews already loaded here.
  useEffect(() => {
    setSearchFailed(false);
    if (!trimmedQuery) {
      setSearchResults(null);
      return;
    }
    const abortController = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const matches: Conversation[] = [];
        let hasMore = true;
        while (hasMore) {
          const params = new URLSearchParams({
            interface_type: 'web',
            limit: '100',
            offset: String(matches.length),
            q: trimmedQuery,
          });
          const response = await fetch(`/api/v1/chat/conversations?${params}`, {
            signal: abortController.signal,
          });
          if (!response.ok) {
            throw new Error(`Conversation search failed with status ${response.status}`);
          }
          const data: { conversations: Conversation[]; count: number } = await response.json();
          matches.push(...data.conversations);
          hasMore = data.conversations.length > 0 && matches.length < data.count;
        }
        setSearchResults({ query: trimmedQuery, conversations: matches });
      } catch (error) {
        if (!abortController.signal.aborted) {
          console.error('Conversation search failed:', error);
          setSearchFailed(true);
        }
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      abortController.abort();
    };
  }, [trimmedQuery]);

  const isAwaitingSearch =
    trimmedQuery !== '' && searchResults?.query !== trimmedQuery && !searchFailed;
  // While a refined query is in flight, the previous query's matches stay on
  // screen (dimmed) rather than blanking the list on every keystroke. Once the
  // query has its own answer — results or a failure — only that is shown.
  // Home and its threads have rows of their own above, so "Earlier" holds
  // only ordinary conversations, including one optimistically inserted on send.
  let displayedConversations: Conversation[] = home
    ? conversations.filter(
        (conv) =>
          (conv.kind ?? 'conversation') === 'conversation' &&
          conv.conversation_id !== home.homeConversationId &&
          !home.threads.some((thread) => thread.conversation_id === conv.conversation_id)
      )
    : conversations;
  if (trimmedQuery) {
    displayedConversations =
      searchResults?.query === trimmedQuery || isAwaitingSearch
        ? (searchResults?.conversations ?? [])
        : [];
  }
  const showSearchingPlaceholder = isAwaitingSearch && displayedConversations.length === 0;

  // Format timestamp for display
  const formatTimestamp = (timestamp: string): string => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) {
      return 'Just now';
    }
    if (diffMins < 60) {
      return `${diffMins}m ago`;
    }
    if (diffHours < 24) {
      return `${diffHours}h ago`;
    }
    if (diffDays < 7) {
      return `${diffDays}d ago`;
    }
    return date.toLocaleDateString();
  };

  const renderConversation = (conv: Conversation) => {
    const isActive = conv.conversation_id === currentConversationId;
    return (
      <button
        key={conv.conversation_id}
        className={`w-full text-left px-3 py-2.5 rounded-lg transition-all duration-150 ${
          isActive ? 'bg-primary/10 text-primary' : 'hover:bg-muted/60 text-foreground'
        }`}
        onClick={() => onConversationSelect(conv.conversation_id)}
        data-testid={`conversation-item-${conv.conversation_id}`}
        data-conversation-id={conv.conversation_id}
      >
        <div className="text-sm leading-snug line-clamp-2 break-words mb-1">
          {conv.summary || conv.last_message}
        </div>
        {conv.match_excerpt && (
          <div
            className="text-xs leading-snug text-muted-foreground line-clamp-2 break-words mb-1"
            data-testid="conversation-match-excerpt"
          >
            {conv.match_excerpt}
          </div>
        )}
        <div className="flex justify-between items-center">
          <span className="text-[11px] text-muted-foreground/70">
            {formatTimestamp(conv.last_timestamp)}
          </span>
          {conv.message_count > 1 && (
            <Badge variant="secondary" className="text-[10px] px-1.5 py-0 h-4 font-normal">
              {conv.message_count}
            </Badge>
          )}
        </div>
      </button>
    );
  };

  const waitingCount = home
    ? home.threads.filter((thread) => thread.state === 'waiting').length
    : 0;
  const homeContent = home && (
    <div className="space-y-3" data-testid="home-sidebar">
      <button
        className={`flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition-all duration-150 ${
          home.homeConversationId === currentConversationId
            ? 'bg-primary/10 text-primary'
            : 'hover:bg-muted/60 text-foreground'
        }`}
        onClick={() => onConversationSelect(home.homeConversationId)}
        data-testid="home-item"
        data-conversation-id={home.homeConversationId}
      >
        <HomeIcon size={14} aria-hidden="true" />
        <span className="flex-1">Home</span>
        {waitingCount > 0 && (
          <span
            className="shrink-0 rounded-full bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:text-violet-300"
            title={`${waitingCount} ${waitingCount === 1 ? 'thread is' : 'threads are'} waiting on you`}
            data-testid="home-waiting-count"
          >
            {waitingCount} waiting
          </span>
        )}
      </button>
      <button
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm text-muted-foreground transition-all duration-150 hover:bg-muted/60 hover:text-foreground"
        onClick={home.onNewThread}
        data-testid="new-thread-button"
      >
        <GitBranchPlusIcon size={14} aria-hidden="true" />
        <span className="flex-1">New thread</span>
      </button>
      {home.threadsFailed && (
        <div
          role="alert"
          className="flex items-center gap-2 px-3 text-xs text-destructive"
          data-testid="threads-load-failed"
        >
          <span className="flex-1">Couldn't load threads.</span>
          <Button variant="outline" size="sm" className="h-6 px-2" onClick={home.onRetryThreads}>
            Retry
          </Button>
        </div>
      )}
      {THREAD_GROUPS.map((state) => {
        const threads = home.threads.filter((thread) => thread.state === state);
        if (threads.length === 0) {
          return null;
        }
        return (
          <section key={state} data-testid={`thread-group-${state}`}>
            <h3 className="flex items-center justify-between px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              <span>{THREAD_STATE_LABELS[state]}</span>
              <span>{threads.length}</span>
            </h3>
            <div className="space-y-0.5">
              {threads.map((thread) => (
                <button
                  key={thread.conversation_id}
                  className={`w-full rounded-lg px-3 py-2 text-left transition-all duration-150 ${
                    thread.conversation_id === currentConversationId
                      ? 'bg-primary/10 text-primary'
                      : 'hover:bg-muted/60 text-foreground'
                  }`}
                  onClick={() => onConversationSelect(thread.conversation_id)}
                  data-testid={`thread-item-${thread.conversation_id}`}
                  data-conversation-id={thread.conversation_id}
                  data-thread-state={thread.state}
                  data-closed-by={thread.closed_by ?? undefined}
                >
                  <span className="flex min-w-0 items-center gap-2 text-sm">
                    <ThreadStateDot state={thread.state} />
                    <span
                      className={`min-w-0 flex-1 truncate ${
                        state === 'done' ? 'text-muted-foreground' : ''
                      }`}
                    >
                      {thread.title}
                    </span>
                    <ThreadUnreadBadge count={thread.unread_count} />
                  </span>
                  {thread.state === 'done' && thread.closed_by === 'user' ? (
                    <span
                      className="mt-0.5 block truncate pl-4 text-xs italic text-muted-foreground"
                      data-testid="thread-item-status"
                    >
                      {threadStatusText(thread)}
                    </span>
                  ) : (
                    thread.preview && (
                      <span
                        className={`mt-0.5 block truncate pl-4 text-xs ${
                          state === 'failed' ? 'text-destructive/80' : 'text-muted-foreground'
                        }`}
                      >
                        {thread.preview}
                      </span>
                    )
                  )}
                </button>
              ))}
            </div>
          </section>
        );
      })}
      <h3 className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        Earlier
      </h3>
    </div>
  );

  const sidebarContent = (
    <div className="flex h-full flex-col min-h-0">
      <div className="flex items-center gap-2 px-4 pb-3 pt-4">
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider flex-1">
          Conversations
        </h2>
        <Button
          onClick={onNewChat}
          aria-label="Start new chat"
          data-testid="new-chat-button"
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0 rounded-lg hover:bg-primary/10 hover:text-primary"
        >
          <MessageSquarePlusIcon size={18} />
        </Button>
      </div>

      <div className="px-4 pb-3">
        <div className="relative">
          <SearchIcon
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground/50"
          />
          <input
            type="search"
            aria-label="Search conversations"
            placeholder="Search..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full h-8 pl-8 pr-3 text-sm rounded-lg border border-border/60 bg-muted/30 placeholder:text-muted-foreground/40 focus:outline-hidden focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all"
          />
        </div>
      </div>

      <ScrollArea className="flex-1 px-2 overflow-y-auto">
        <div className="py-1">
          {searchFailed && (
            <div
              className="px-3 py-2 text-xs text-destructive"
              data-testid="conversation-search-failed"
            >
              Search failed. Try again.
            </div>
          )}
          {!trimmedQuery && homeContent}
          {(conversationsLoading && !trimmedQuery) || showSearchingPlaceholder ? (
            <div
              className="py-8 text-center text-sm text-muted-foreground/60"
              data-loading-indicator="true"
            >
              {showSearchingPlaceholder ? 'Searching...' : 'Loading...'}
            </div>
          ) : displayedConversations.length === 0 ? (
            !searchFailed && (
              <div className="py-8 text-center text-sm text-muted-foreground/60">
                {trimmedQuery
                  ? 'No matches'
                  : home
                    ? 'No earlier conversations'
                    : 'No conversations yet'}
              </div>
            )
          ) : (
            <div
              className={`space-y-0.5 transition-opacity ${isAwaitingSearch ? 'opacity-60' : ''}`}
              aria-busy={isAwaitingSearch}
            >
              {displayedConversations.map(renderConversation)}
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );

  // On mobile, render as a full-page view (parent controls visibility)
  if (isMobile) {
    return <div className="h-full w-full bg-background">{sidebarContent}</div>;
  }

  // Desktop: collapsible sidebar panel
  return (
    <div
      className={`h-full w-72 flex-shrink-0 border-r border-border/50 bg-muted/30 transition-all duration-300 ${
        isOpen ? 'ml-0' : '-ml-72'
      }`}
    >
      {sidebarContent}
    </div>
  );
};

export default ConversationSidebar;
