import {
  ActionBarPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useComposer,
  useComposerRuntime,
  useMessage,
  useThread,
} from '@assistant-ui/react';
import {
  ArrowDownIcon,
  ArrowUpIcon,
  BotIcon,
  CalendarIcon,
  CheckCircle2Icon,
  CheckIcon,
  CopyIcon,
  FileSearchIcon,
  Loader2Icon,
  StickyNoteIcon,
  SquareIcon,
} from 'lucide-react';
import React, { useState } from 'react';
import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from '@/components/assistant-ui/attachment';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { AssistantResponseImages } from './AssistantResponseImages';
import { useChatControls } from './chatControls';
import { LOADING_MARKER } from './constants';
import { DynamicToolUI } from './DynamicToolUI';
import { MarkdownText } from './MarkdownText';
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
  return (
    <ThreadPrimitive.Root className="flex flex-1 flex-col min-h-0">
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto overflow-x-hidden scrollbar-thin scrollbar-thumb-muted-foreground/20 min-h-0">
        <div className="pb-6">
          <ThreadWelcome />

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

        <ThreadScrollToBottom />
      </ThreadPrimitive.Viewport>

      <div
        className="flex-shrink-0 border-t border-border/50 bg-background/80 backdrop-blur-sm px-4 py-3 md:px-6 md:py-4"
        data-testid="composer-container"
      >
        <Composer />
      </div>
    </ThreadPrimitive.Root>
  );
};

const ThreadScrollToBottom: React.FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      {/* @ts-expect-error - TooltipIconButton JSX component */}
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="absolute bottom-2 right-4 md:right-8 z-10 rounded-full shadow-md bg-background/90 backdrop-blur-sm border-border/50 opacity-0 scale-75 transition-all duration-200 data-[enabled]:opacity-100 data-[enabled]:scale-100 h-8 w-8"
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
  const isRunning = useThread((t) => t.isRunning);
  const composerRuntime = useComposerRuntime();
  const controls = useChatControls();
  const [steering, setSteering] = useState(false);

  // While a turn is running the main composer doubles as the steer input: the
  // typed message is injected mid-turn and the model adapts without restarting.
  // Cleared on accept/finished; kept on error so the user can retry.
  const submitSteer = async () => {
    if (!controls || steering) {
      return;
    }
    const text = composerRuntime.getState().text.trim();
    if (!text) {
      return;
    }
    setSteering(true);
    try {
      const result = await controls.submitSteer(text);
      // Clear on success, but only if the user hasn't typed something new while
      // the steer was in flight (don't clobber a fresh edit).
      if (result !== 'error' && composerRuntime.getState().text.trim() === text) {
        composerRuntime.setText('');
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
      </div>
    </ComposerPrimitive.Root>
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
  const isSending = useComposer((state) => !state.canSend && !state.isEmpty);
  // While running, the single action button steers when there's text to send
  // and stops the turn when the composer is empty.
  const hasText = useComposer((state) => state.text.trim().length > 0);

  return (
    <>
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send asChild>
          {/* @ts-expect-error - TooltipIconButton JSX component */}
          <TooltipIconButton
            tooltip={isSending ? 'Sending...' : 'Send message'}
            variant="default"
            side="top"
            className="h-11 w-11 shrink-0 rounded-full"
            data-testid="send-button"
            disabled={isSending}
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
  const message = useMessage();
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
  };
  const profileId = custom.processing_profile_id;
  const profile = profileId ? profilesById[profileId] : null;
  const reasoningInfo = custom.reasoning_info;
  const tierId = reasoningInfo?.model_tier;
  // The label comes from the configured tiers; an id with no configured label
  // (a tier removed since the turn ran) still names what served the turn.
  const tierLabel = tierId ? (tierLabels[tierId] ?? tierId) : null;
  const hasCopyableContent = hasCopyableAssistantContent(message?.content);

  return (
    <MessagePrimitive.Root
      className="px-4 py-2 md:px-6 animate-in slide-in-from-bottom-2 duration-200 group"
      data-testid="assistant-message"
    >
      <div className="max-w-3xl mx-auto relative">
        <div className="flex items-start gap-2.5">
          <Avatar className="h-8 w-8 shrink-0 mt-0.5">
            <AvatarFallback className="bg-primary/10 border border-primary/20">
              <BotIcon size={16} className="text-primary" />
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
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
              {hasCopyableContent && <AssistantActionBar />}
            </div>
          </div>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: React.FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      autohideFloat="single-branch"
      data-testid="assistant-action-bar"
      className="absolute left-0 top-full z-10 mt-1 flex items-center gap-1 rounded-lg border border-border/50 bg-background/95 p-1 opacity-0 shadow-sm backdrop-blur-sm pointer-events-none transition-opacity duration-200 group-hover:opacity-100 group-hover:pointer-events-auto"
    >
      <ActionBarPrimitive.Copy asChild>
        {/* @ts-expect-error - TooltipIconButton JSX component */}
        <TooltipIconButton tooltip="Copy" size="sm" variant="ghost" className="h-7 w-7 rounded-lg">
          <MessagePrimitive.If copied>
            <CheckIcon size={12} />
          </MessagePrimitive.If>
          <MessagePrimitive.If copied={false}>
            <CopyIcon size={12} />
          </MessagePrimitive.If>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
    </ActionBarPrimitive.Root>
  );
};
