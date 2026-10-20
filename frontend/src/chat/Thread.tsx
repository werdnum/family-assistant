import {
  ActionBarPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useThreadViewport,
} from '@assistant-ui/react';
import {
  ArrowDownIcon,
  ArrowUpIcon,
  BotIcon,
  CalendarIcon,
  CheckCircle2Icon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CopyIcon,
  FileSearchIcon,
  FolderInputIcon,
  GitBranchPlusIcon,
  Loader2Icon,
  StickyNoteIcon,
  SquareIcon,
} from 'lucide-react';
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from '@/components/assistant-ui/attachment';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { AssistantResponseImages } from './AssistantResponseImages';
import { type ConversationLoadStatus, useChatControls } from './chatControls';
import { LOADING_MARKER } from './constants';
import { DynamicToolUI } from './DynamicToolUI';
import { useHomeTurnActions } from './homeTurnActions';
import { MarkdownText } from './MarkdownText';
import { PendingDelegationsChip } from './PendingDelegationsChip';
import { ThreadEchoHeader } from './ThreadEchoHeader';
import { useThreadsContext } from './threadsContext';
import { useProfiles } from './profilesContext';
import { ToolGroup } from './ToolGroup';
import { TooltipIconButton } from './TooltipIconButton';
import type { MessageReasoningInfo } from './types';

const messageContentComponents = {
  Text: MarkdownText,
  ToolGroup, // ToolGroup should be at root level, not nested under tools
  tools: {
    // Use DynamicToolUI as the fallback which will handle all tools
    Fallback: DynamicToolUI,
    // Don't specify by_name since we want all tools to go through DynamicToolUI
  },
};

export const Thread: React.FC = () => {
  return <ThreadContent />;
};

const ThreadContent: React.FC = () => {
  const viewportRef = useRef<HTMLDivElement>(null);
  const conversationLoadStatus = useChatControls()?.conversationLoadStatus ?? null;
  return (
    <ThreadPrimitive.Root className="flex flex-1 flex-col min-h-0">
      <div className="relative flex min-h-0 flex-1 flex-col">
        <ThreadPrimitive.Viewport
          ref={viewportRef}
          data-testid="thread-viewport"
          className="flex-1 overflow-y-auto overflow-x-hidden scrollbar-thin scrollbar-thumb-muted-foreground/20 min-h-0"
        >
          <div className="pb-6">
            {conversationLoadStatus ? (
              <ThreadConversationLoad status={conversationLoadStatus} />
            ) : (
              <ThreadWelcome />
            )}

            <ThreadLoadOlderMessages viewportRef={viewportRef} />

            <ThreadPrimitive.Messages
              components={{
                UserMessage: UserMessage,
                EditComposer: EditComposer,
                AssistantMessage: AssistantMessage,
              }}
            />

            <ThreadPrimitive.If empty={false}>
              <div className="h-4" />
            </ThreadPrimitive.If>
          </div>
        </ThreadPrimitive.Viewport>
        <ThreadScrollToBottom />
        <ReportViewportAtBottom />
      </div>

      <div
        className="flex-shrink-0 border-t border-border/50 bg-background/80 backdrop-blur-sm px-4 py-3 md:px-6 md:py-4"
        data-testid="composer-container"
      >
        <ThreadPendingDelegations />
        <Composer />
      </div>
    </ThreadPrimitive.Root>
  );
};

const ThreadLoadOlderMessages: React.FC<{
  viewportRef: React.RefObject<HTMLDivElement | null>;
}> = ({ viewportRef }) => {
  const controls = useChatControls();
  const isRunning = useAuiState((s) => s.thread.isRunning);
  const messageCount = useAuiState((s) => s.thread.messages.length);
  // Older messages arrive above the reader, which would push what they were
  // looking at down the page. Remember the distance from the bottom when they
  // ask, and restore it once the longer thread renders.
  const pendingAnchorRef = useRef<{ messageCount: number; fromBottom: number } | null>(null);
  const status = controls?.olderMessagesStatus;

  useLayoutEffect(() => {
    const anchor = pendingAnchorRef.current;
    const viewport = viewportRef.current;
    if (!anchor || !viewport) {
      return;
    }
    if (messageCount !== anchor.messageCount) {
      pendingAnchorRef.current = null;
      viewport.scrollTop = viewport.scrollHeight - anchor.fromBottom;
    } else if (status === 'failed') {
      pendingAnchorRef.current = null;
    }
  }, [messageCount, status, viewportRef]);

  if (!controls?.hasOlderMessages) {
    return null;
  }

  const handleClick = () => {
    const viewport = viewportRef.current;
    if (viewport) {
      pendingAnchorRef.current = {
        messageCount,
        fromBottom: viewport.scrollHeight - viewport.scrollTop,
      };
    }
    controls.loadOlderMessages();
  };

  return (
    <div className="flex justify-center pt-4">
      <Button
        variant="outline"
        size="sm"
        className="rounded-full text-muted-foreground"
        onClick={handleClick}
        disabled={isRunning || status === 'loading'}
      >
        {status === 'loading' ? (
          <Loader2Icon size={14} className="animate-spin" />
        ) : (
          <ArrowUpIcon size={14} />
        )}
        {status === 'failed'
          ? "Couldn't load earlier messages. Try again"
          : 'Load earlier messages'}
      </Button>
    </div>
  );
};

// Stands in for the transcript while the open conversation's history is not
// there to show, so the reader is never left looking at nothing — or at the
// welcome screen of a conversation that has history.
const ThreadConversationLoad: React.FC<{ status: ConversationLoadStatus }> = ({ status }) => {
  const controls = useChatControls();
  if (status === 'loading') {
    return (
      <div
        className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-muted-foreground"
        role="status"
        data-testid="conversation-loading"
      >
        <Loader2Icon size={16} className="animate-spin" />
        Loading conversation…
      </div>
    );
  }
  return (
    <div
      className="flex min-h-[50vh] flex-col items-center justify-center gap-3 px-6 text-center"
      role="alert"
      data-testid="conversation-load-error"
    >
      <p className="text-sm text-muted-foreground">Couldn't load this conversation.</p>
      <Button variant="outline" size="sm" onClick={() => controls?.retryConversationLoad()}>
        Retry
      </Button>
    </div>
  );
};

/** Tells a Home or thread whether its newest message is scrolled into view. */
const ReportViewportAtBottom: React.FC = () => {
  const isAtBottom = useThreadViewport((state) => state.isAtBottom);
  const setViewportAtBottom = useThreadsContext()?.setViewportAtBottom;
  useEffect(() => {
    setViewportAtBottom?.(isAtBottom);
  }, [isAtBottom, setViewportAtBottom]);
  return null;
};

const ThreadScrollToBottom: React.FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      {/* @ts-expect-error - TooltipIconButton JSX component */}
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="absolute bottom-2 right-4 md:right-8 z-10 rounded-full shadow-md bg-background/90 backdrop-blur-sm border-border/50 opacity-0 [&:disabled]:opacity-0 scale-75 transition-all duration-200 data-[enabled]:opacity-100 data-[enabled]:scale-100 h-8 w-8"
      >
        <ArrowDownIcon size={16} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: React.FC = () => {
  return (
    <ThreadPrimitive.Empty>
      <div className="flex min-h-[50vh] md:min-h-[65vh] flex-col items-center justify-center px-6 py-12 animate-in fade-in duration-500">
        <div className="text-center mb-10 animate-in slide-in-from-bottom-4 duration-500">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-primary/10 mb-6">
            <BotIcon size={32} strokeWidth={1.5} className="text-primary" />
          </div>
          <h2 className="text-2xl md:text-3xl font-semibold tracking-tight mb-2">
            How can I help you?
          </h2>
          <p className="text-muted-foreground text-base">
            Ask me anything about your calendar, notes, documents, or tasks.
          </p>
        </div>
        <ThreadWelcomeSuggestions />
      </div>
    </ThreadPrimitive.Empty>
  );
};

const ThreadWelcomeSuggestions: React.FC = () => {
  const suggestions = [
    {
      prompt: "What's on my calendar today?",
      label: 'Calendar',
      icon: CalendarIcon,
      color: 'text-blue-500',
      bgColor: 'bg-blue-500/10',
    },
    {
      prompt: 'Add a note about groceries',
      label: 'Notes',
      icon: StickyNoteIcon,
      color: 'text-amber-500',
      bgColor: 'bg-amber-500/10',
    },
    {
      prompt: 'Search my documents for recipes',
      label: 'Documents',
      icon: FileSearchIcon,
      color: 'text-purple-500',
      bgColor: 'bg-purple-500/10',
    },
    {
      prompt: 'What tasks do I have pending?',
      label: 'Tasks',
      icon: CheckCircle2Icon,
      color: 'text-emerald-500',
      bgColor: 'bg-emerald-500/10',
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-xl animate-in slide-in-from-bottom-6 duration-600 delay-200">
      {suggestions.map((suggestion, index) => {
        const Icon = suggestion.icon;
        return (
          <ThreadPrimitive.Suggestion
            key={index}
            className="flex items-center gap-3 px-4 py-3.5 text-left rounded-xl border border-border/60 bg-card hover:bg-accent/50 hover:border-border transition-all duration-200 hover:shadow-sm cursor-pointer group"
            prompt={suggestion.prompt}
            method="replace"
            autoSend
          >
            <div
              className={`flex items-center justify-center w-9 h-9 rounded-lg ${suggestion.bgColor} shrink-0`}
            >
              <Icon size={18} className={suggestion.color} />
            </div>
            <div className="min-w-0">
              <div className="text-xs font-medium text-muted-foreground mb-0.5">
                {suggestion.label}
              </div>
              <div className="text-sm font-medium">{suggestion.prompt}</div>
            </div>
          </ThreadPrimitive.Suggestion>
        );
      })}
    </div>
  );
};

// A steer that failed or could not be confirmed, shown just above the composer
// (which keeps its text so the user can retry). Deliberately NOT gated on the
// turn still running: a turn whose stream gave up reports back here with the
// text it could not confirm, and that has to stay readable after the turn ends.
const ThreadPendingDelegations: React.FC = () => {
  const controls = useChatControls();
  const delegations = controls?.pendingDelegations ?? [];
  if (delegations.length === 0) {
    return null;
  }
  return (
    <div className="pb-2">
      <PendingDelegationsChip delegations={delegations} />
    </div>
  );
};

// Cleared when the user sends, steers again, or switches conversation.
const SteerError: React.FC = () => {
  const controls = useChatControls();
  if (!controls?.steerError) {
    return null;
  }
  return (
    <p className="px-2 text-xs text-destructive" data-testid="steer-error">
      {controls.steerError}
    </p>
  );
};

const Composer: React.FC = () => {
  const aui = useAui();
  const isRunning = useAuiState((s) => s.thread.isRunning);
  const controls = useChatControls();
  const [steering, setSteering] = useState(false);

  // While a turn is running the main composer doubles as the steer input: the
  // typed message is injected mid-turn and the model adapts without restarting.
  // Cleared on accept/finished; kept on error so the user can retry.
  const submitSteer = async () => {
    if (!controls || steering) {
      return;
    }
    const text = aui.composer.getState().text.trim();
    if (!text) {
      return;
    }
    setSteering(true);
    try {
      const result = await controls.submitSteer(text);
      // Clear on success, but only if the user hasn't typed something new while
      // the steer was in flight (don't clobber a fresh edit).
      if (result !== 'error' && aui.composer.getState().text.trim() === text) {
        aui.composer.setText('');
      }
    } finally {
      setSteering(false);
    }
  };

  // While running, Enter steers the turn instead of starting a new one; when
  // idle, fall through to the composer's default submit-on-enter. Skip while an
  // IME composition is active so committing composing text with Enter doesn't
  // steer mid-composition (mirrors assistant-ui's own default Enter handler).
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!isRunning || e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) {
      return;
    }
    e.preventDefault();
    void submitSteer();
  };

  return (
    <ComposerPrimitive.Root className="flex flex-col gap-3 max-w-3xl mx-auto w-full">
      <SteerError />
      {controls?.sendAsThread?.error && (
        <p
          role="alert"
          className="px-2 text-xs text-destructive"
          data-testid="send-as-thread-error"
        >
          {controls.sendAsThread.error}
        </p>
      )}
      {/* Steering sends text only, so attachments can't ride along on a steer.
          Hide the attachment UI while a turn runs to avoid a picked file being
          silently ignored by the steer and then sent with the next message. */}
      {!isRunning && <ComposerAttachments />}
      <div className="flex gap-2 items-end">
        {!isRunning && <ComposerAddAttachment />}
        <div className="flex-1 relative">
          <ComposerPrimitive.Input
            rows={1}
            autoFocus
            placeholder={
              isRunning ? 'Steer the assistant while it works…' : 'Message Family Assistant...'
            }
            onKeyDown={handleKeyDown}
            className="w-full min-h-11 max-h-48 pl-4 pr-4 py-2.5 text-sm border rounded-2xl bg-muted/40 border-border/60 resize-none focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-200 placeholder:text-muted-foreground/60"
            data-testid="chat-input"
          />
        </div>
        <ComposerAction steering={steering} onSteer={submitSteer} />
        {controls?.sendAsThread && (
          <ThreadPrimitive.If running={false}>
            <SendAsThreadMenu
              profiles={controls.sendAsThread.profiles}
              onSend={(profileId) => {
                controls.sendAsThread?.prepare(profileId);
                void aui.composer.send();
              }}
            />
          </ThreadPrimitive.If>
        )}
      </div>
    </ComposerPrimitive.Root>
  );
};

/**
 * Sends what the composer holds as a new thread's opening message, on the
 * profile picked here, instead of as a Home turn.
 */
const SendAsThreadMenu: React.FC<{
  profiles: { id: string; label: string }[];
  onSend: (profileId: string) => void;
}> = ({ profiles, onSend }) => {
  const canSend = useAuiState((s) => s.composer.canSend && s.composer.text.trim().length > 0);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className="h-11 shrink-0 gap-1 rounded-full px-3"
          disabled={!canSend || profiles.length === 0}
          aria-label="Send as a new thread"
          title="Send as a new thread"
          data-testid="send-as-thread-button"
        >
          <GitBranchPlusIcon size={16} aria-hidden="true" />
          <ChevronDownIcon size={12} aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" side="top">
        <DropdownMenuLabel>Send as a new thread on</DropdownMenuLabel>
        {profiles.map((profile) => (
          <DropdownMenuItem
            key={profile.id}
            onSelect={() => onSend(profile.id)}
            data-testid={`send-as-thread-${profile.id}`}
          >
            {profile.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
};

interface ComposerActionProps {
  steering: boolean;
  onSteer: () => void;
}

const ComposerAction: React.FC<ComposerActionProps> = ({ steering, onSteer }) => {
  // The composer refuses to send while it is already sending, which is the
  // window in which attachments upload. Reading that rather than attachment
  // status keeps the button honest whatever an attachment adapter reports.
  // Sending is also refused while the open conversation's history loads, which
  // is not a send in progress.
  const conversationLoading = (useChatControls()?.conversationLoadStatus ?? null) !== null;
  const isSending =
    useAuiState((s) => !s.composer.canSend && !s.composer.isEmpty) && !conversationLoading;
  // While running, the single action button steers when there's text to send
  // and stops the turn when the composer is empty.
  const hasText = useAuiState((s) => s.composer.text.trim().length > 0);

  return (
    <>
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send asChild>
          {/* @ts-expect-error - TooltipIconButton JSX component */}
          <TooltipIconButton
            tooltip={isSending ? 'Sending...' : 'Send message'}
            aria-label="Send message"
            variant="default"
            side="top"
            className="h-11 w-11 shrink-0 rounded-full"
            data-testid="send-button"
            disabled={isSending || conversationLoading}
          >
            {isSending ? (
              <Loader2Icon size={16} className="animate-spin" />
            ) : (
              <ArrowUpIcon size={16} />
            )}
          </TooltipIconButton>
        </ComposerPrimitive.Send>
      </ThreadPrimitive.If>
      <ThreadPrimitive.If running>
        {hasText ? (
          /* @ts-expect-error - TooltipIconButton JSX component */
          <TooltipIconButton
            type="button"
            tooltip="Steer the assistant"
            variant="default"
            side="top"
            className="h-11 w-11 shrink-0 rounded-full"
            data-testid="steer-button"
            disabled={steering}
            onClick={onSteer}
          >
            {steering ? (
              <Loader2Icon size={16} className="animate-spin" />
            ) : (
              <ArrowUpIcon size={16} />
            )}
          </TooltipIconButton>
        ) : (
          <ComposerPrimitive.Cancel asChild>
            {/* @ts-expect-error - TooltipIconButton JSX component */}
            <TooltipIconButton
              tooltip="Stop generating"
              variant="default"
              side="top"
              className="h-11 w-11 shrink-0 rounded-full"
              data-testid="stop-button"
            >
              <SquareIcon size={14} />
            </TooltipIconButton>
          </ComposerPrimitive.Cancel>
        )}
      </ThreadPrimitive.If>
    </>
  );
};

const UserMessage: React.FC = () => {
  return (
    <MessagePrimitive.Root
      className="px-4 py-2 md:px-6 animate-in slide-in-from-bottom-2 duration-200 group"
      data-testid="user-message"
    >
      <div className="max-w-3xl mx-auto relative">
        <UserMessageAttachments />
        <div className="flex items-end gap-2.5 justify-end min-w-0">
          <div
            className="max-w-full md:max-w-[75%] min-w-0 px-4 py-2.5 bg-primary text-primary-foreground rounded-2xl rounded-br-md shadow-sm overflow-x-auto"
            data-testid="user-message-content"
          >
            <MessagePrimitive.Parts />
          </div>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};

const EditComposer: React.FC = () => {
  return (
    <Card className="p-4 m-2">
      <ComposerPrimitive.Root className="space-y-3">
        <ComposerPrimitive.Input
          className="w-full p-3 border rounded-lg bg-background min-h-[60px] resize-y focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
          autoFocus
        />
        <div className="flex gap-2 justify-end">
          <ComposerPrimitive.Cancel asChild>
            <Button variant="outline" size="sm">
              Cancel
            </Button>
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send asChild>
            <Button size="sm">Save</Button>
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </Card>
  );
};

export function hasCopyableAssistantContent(content: unknown): boolean {
  if (typeof content === 'string') {
    return content.trim().length > 0 && content !== LOADING_MARKER;
  }

  if (!Array.isArray(content)) {
    return false;
  }

  return content.some((part: unknown) => {
    if (typeof part !== 'object' || part === null) {
      return false;
    }

    const typedPart = part as Record<string, unknown>;
    return (
      typedPart.type === 'text' &&
      typeof typedPart.text === 'string' &&
      typedPart.text.trim().length > 0 &&
      typedPart.text !== LOADING_MARKER
    );
  });
}

const AssistantMessage: React.FC = () => {
  const message = useAuiState((s) => s.message);
  const { profilesById, tierLabels, error } = useProfiles();

  // Check if message is loading by checking for our special marker
  // The assistant-ui library might not pass through our custom isLoading property
  const isLoading =
    Array.isArray(message?.content) &&
    message.content.length > 0 &&
    message.content[0]?.text === LOADING_MARKER;

  // Which profile answered and what served the turn. assistant-ui reconstructs
  // each message from the fields it knows, so ours arrive in metadata.custom.
  const custom = (message?.metadata?.custom ?? {}) as {
    processing_profile_id?: string;
    reasoning_info?: MessageReasoningInfo;
    echoed_from_thread?: string;
    moved_to_thread?: string;
    internal_id?: number;
    turn_id?: string;
  };
  if (custom.moved_to_thread) {
    return <MovedTurnMarker threadConversationId={custom.moved_to_thread} />;
  }
  // An echo of a thread's outcome is attributed to the thread rather than to
  // the Home profile and turn its copy was stored under.
  const echoedFromThread = custom.echoed_from_thread;
  const profileId = echoedFromThread ? undefined : custom.processing_profile_id;
  const profile = profileId ? profilesById[profileId] : null;
  const reasoningInfo = echoedFromThread ? undefined : custom.reasoning_info;
  const tierId = reasoningInfo?.model_tier;
  // The label comes from the configured tiers; an id with no configured label
  // (a tier removed since the turn ran) still names what served the turn.
  const tierLabel = tierId ? (tierLabels[tierId] ?? tierId) : null;
  const hasCopyableContent = hasCopyableAssistantContent(message?.content);

  return (
    <MessagePrimitive.Root
      className="px-4 py-2 md:px-6 animate-in slide-in-from-bottom-2 duration-200 group"
      data-testid="assistant-message"
      data-echoed-from-thread={echoedFromThread}
    >
      <div className="max-w-3xl mx-auto relative">
        <div className="flex items-start gap-2.5">
          <Avatar className="h-8 w-8 shrink-0 mt-0.5">
            <AvatarFallback className="bg-primary/10 border border-primary/20">
              <BotIcon size={16} className="text-primary" />
            </AvatarFallback>
          </Avatar>
          <div
            className={
              echoedFromThread
                ? 'flex-1 min-w-0 rounded-r-lg border-l-2 border-emerald-500/60 bg-muted/30 py-1.5 pl-3 pr-2'
                : 'flex-1 min-w-0'
            }
          >
            {echoedFromThread && <ThreadEchoHeader threadConversationId={echoedFromThread} />}
            {profile && (
              <Badge
                variant="secondary"
                className="text-[10px] px-1.5 py-0 mb-1 font-normal"
                title={`Generated by ${profile.description}`}
              >
                {profile.description}
              </Badge>
            )}
            {profileId && !profile && !error && (
              <Badge
                variant="outline"
                className="text-[10px] px-1.5 py-0 mb-1 opacity-50"
                title="Profile information loading..."
              >
                Loading...
              </Badge>
            )}
            {profileId && !profile && error && (
              <Badge
                variant="destructive"
                className="text-[10px] px-1.5 py-0 mb-1 opacity-70"
                title={`Profile unavailable: ${error}`}
              >
                Profile Error
              </Badge>
            )}
            {tierLabel && (
              <Badge
                variant="secondary"
                className="text-[10px] px-1.5 py-0 mb-1 ml-1 font-normal"
                data-testid="model-tier-badge"
                title={
                  reasoningInfo?.model ? `${tierLabel} · ${reasoningInfo.model}` : `${tierLabel}`
                }
              >
                {tierLabel}
                {reasoningInfo?.model_tier_source === 'user' && (
                  <span className="ml-1 opacity-60">chosen</span>
                )}
              </Badge>
            )}
            <div className="relative">
              <div className="prose-sm overflow-x-auto" data-testid="assistant-message-content">
                {isLoading ? (
                  <div className="flex items-center gap-1.5 py-2">
                    <div className="w-1.5 h-1.5 bg-primary/40 rounded-full animate-bounce [animation-delay:-0.32s]"></div>
                    <div className="w-1.5 h-1.5 bg-primary/40 rounded-full animate-bounce [animation-delay:-0.16s]"></div>
                    <div className="w-1.5 h-1.5 bg-primary/40 rounded-full animate-bounce"></div>
                  </div>
                ) : (
                  <>
                    {Array.isArray(message.content) ? (
                      // @ts-expect-error - assistant-ui tool type mismatch
                      <MessagePrimitive.Parts components={messageContentComponents} />
                    ) : typeof message.content === 'string' ? (
                      <MarkdownText text={message.content} />
                    ) : message.content ? (
                      <MarkdownText text={String(message.content)} />
                    ) : (
                      <div className="text-muted-foreground italic">No content</div>
                    )}
                  </>
                )}
              </div>
              {/* Images the assistant attached, shown inline rather than only
                  behind the collapsed attachments tool group. */}
              <AssistantResponseImages />
              {hasCopyableContent && (
                <AssistantActionBar
                  internalId={custom.internal_id}
                  turnId={custom.turn_id}
                  isEcho={Boolean(echoedFromThread)}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};

/** Where a Home turn the user moved went: a link to the thread that holds it now. */
const MovedTurnMarker: React.FC<{ threadConversationId: string }> = ({ threadConversationId }) => {
  const threads = useThreadsContext();
  const thread = threads?.threadsByConversationId.get(threadConversationId);
  const label = thread ? (
    <>
      Moved to <span className="font-medium text-foreground/90">{thread.title}</span>
    </>
  ) : (
    'Moved to a thread'
  );
  return (
    <MessagePrimitive.Root
      className="px-4 py-1 md:px-6"
      data-testid="moved-turn-marker"
      data-moved-to-thread={threadConversationId}
    >
      <div className="max-w-3xl mx-auto flex justify-center">
        {threads ? (
          <button
            type="button"
            onClick={() => threads.openConversation(threadConversationId)}
            className="flex min-w-0 items-center gap-1.5 rounded-full border border-dashed border-border/70 px-3 py-1 text-xs text-muted-foreground hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={thread ? `Open thread: ${thread.title}` : 'Open the thread it moved to'}
            data-testid="moved-turn-link"
          >
            <FolderInputIcon size={12} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 truncate">{label}</span>
            <ChevronRightIcon size={12} className="shrink-0" aria-hidden="true" />
          </button>
        ) : (
          <span className="text-xs text-muted-foreground">{label}</span>
        )}
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: React.FC<{
  internalId?: number;
  turnId?: string;
  isEcho: boolean;
}> = ({ internalId, turnId, isEcho }) => {
  const aui = useAui();
  const homeActions = useHomeTurnActions();
  // Only an exchange the user started moves; an echo stays in Home.
  const canMove = homeActions !== null && !isEcho && homeActions.turnHasUserMessage(turnId);
  const target = () => ({ internalId, turnId, quote: aui.message().getCopyText() });
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 3000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copyResponse = async () => {
    setCopied(false);
    setCopyError(false);
    const text = aui.message().getCopyText();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      setCopyError(true);
      return;
    }
    setCopied(true);
  };

  return (
    <>
      {copyError && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          Couldn't copy response. Select the text and copy it manually.
        </p>
      )}
      <span role="status" className="sr-only">
        {copied ? 'Response copied to clipboard.' : ''}
      </span>
      {/* CSS hiding keeps older replies in the Tab order; primitive autohide unmounts them. */}
      <ActionBarPrimitive.Root
        hideWhenRunning
        autohide="never"
        data-testid="assistant-action-bar"
        className="absolute left-0 top-full z-10 mt-1 flex items-center gap-1 rounded-lg border border-border/50 bg-background/95 p-1 opacity-0 shadow-sm backdrop-blur-sm pointer-events-none transition-opacity duration-200 group-hover:opacity-100 group-hover:pointer-events-auto focus-within:opacity-100 focus-within:pointer-events-auto"
      >
        {/* @ts-expect-error - TooltipIconButton JSX component */}
        <TooltipIconButton
          tooltip={copied ? 'Copied' : 'Copy'}
          aria-label="Copy response"
          size="icon"
          variant="ghost"
          className="h-7 w-7 rounded-lg"
          onClick={copyResponse}
        >
          {copied ? (
            <CheckIcon size={12} aria-hidden="true" />
          ) : (
            <CopyIcon size={12} aria-hidden="true" />
          )}
        </TooltipIconButton>
        {homeActions && (internalId !== undefined || turnId) && (
          <>
            {/* @ts-expect-error - TooltipIconButton JSX component */}
            <TooltipIconButton
              tooltip="Continue in a thread"
              aria-label="Continue in a thread"
              size="icon"
              variant="ghost"
              className="h-7 w-7 rounded-lg"
              onClick={() => homeActions.continueInThread(target())}
              data-testid="continue-in-thread"
            >
              <GitBranchPlusIcon size={12} aria-hidden="true" />
            </TooltipIconButton>
            {canMove && (
              /* @ts-expect-error - TooltipIconButton JSX component */
              <TooltipIconButton
                tooltip="Move to thread…"
                aria-label="Move to thread"
                size="icon"
                variant="ghost"
                className="h-7 w-7 rounded-lg"
                onClick={() => homeActions.moveToThread(target())}
                data-testid="move-to-thread"
              >
                <FolderInputIcon size={12} aria-hidden="true" />
              </TooltipIconButton>
            )}
          </>
        )}
      </ActionBarPrimitive.Root>
    </>
  );
};
