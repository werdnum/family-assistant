import React from 'react';
import {
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
} from '@assistant-ui/react';
import {
  ArrowDownIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  PencilIcon,
  RefreshCwIcon,
  SendHorizontalIcon,
  UserIcon,
  BotIcon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import classNames from 'classnames';
import { MarkdownText } from './MarkdownText';
import { TooltipIconButton } from './TooltipIconButton';
import { LOADING_MARKER } from './constants';
import { DynamicToolUI } from './DynamicToolUI';

const messageContentComponents = {
  Text: MarkdownText,
  tools: {
    // Use DynamicToolUI as the fallback which will handle all tools
    Fallback: DynamicToolUI,
    // Don't specify by_name since we want all tools to go through DynamicToolUI
  },
};

interface BranchPickerProps {
  className?: string;
}

export const Thread: React.FC = () => {
  return (
    <ThreadPrimitive.Root className="flex h-full flex-col bg-background">
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-muted-foreground/20">
        <ThreadWelcome />

        <ThreadPrimitive.Messages
          components={{
            UserMessage: UserMessage,
            EditComposer: EditComposer,
            AssistantMessage: AssistantMessage,
          }}
        />

        <ThreadPrimitive.If empty={false}>
          <div className="h-20" />
        </ThreadPrimitive.If>

        <div className="sticky bottom-0 bg-background border-t p-6">
          <ThreadScrollToBottom />
          <Composer />
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadScrollToBottom: React.FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="absolute bottom-20 right-8 z-10 shadow-lg bg-background opacity-0 scale-75 transition-all duration-200 data-[enabled]:opacity-100 data-[enabled]:scale-100"
      >
        <ArrowDownIcon size={16} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: React.FC = () => {
  return (
    <ThreadPrimitive.Empty>
      <div className="flex min-h-[60vh] flex-col items-center justify-center p-8 animate-in fade-in duration-300">
        <Card className="p-8 text-center">
          <div className="mb-6 text-primary animate-in slide-in-from-bottom-4 duration-500 delay-150">
            <BotIcon size={48} strokeWidth={1.5} className="mx-auto animate-bounce" />
          </div>
          <h3 className="text-2xl font-semibold mb-2 animate-in slide-in-from-bottom-4 duration-500 delay-300">
            Welcome to Family Assistant
          </h3>
          <p className="text-lg text-muted-foreground mb-8 animate-in slide-in-from-bottom-4 duration-500 delay-500">
            How can I help you today?
          </p>
          <ThreadWelcomeSuggestions />
        </Card>
      </div>
    </ThreadPrimitive.Empty>
  );
};

const ThreadWelcomeSuggestions: React.FC = () => {
  const suggestions = [
    {
      prompt: "What's on my calendar today?",
      icon: '📅',
    },
    {
      prompt: 'Add a note about groceries',
      icon: '📝',
    },
    {
      prompt: 'Search my documents for recipes',
      icon: '🔍',
    },
    {
      prompt: 'What tasks do I have pending?',
      icon: '✅',
    },
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-2xl animate-in slide-in-from-bottom-8 duration-700 delay-700">
      {suggestions.map((suggestion, index) => (
        <ThreadPrimitive.Suggestion
          key={index}
          className="flex items-center gap-3 p-4 text-left border rounded-lg hover:border-primary hover:bg-accent transition-all duration-200 hover:-translate-y-0.5 hover:shadow-sm cursor-pointer group"
          prompt={suggestion.prompt}
          method="replace"
          autoSend
        >
          <span className="text-2xl group-hover:scale-110 transition-transform duration-200">
            {suggestion.icon}
          </span>
          <span className="text-sm font-medium">{suggestion.prompt}</span>
        </ThreadPrimitive.Suggestion>
      ))}
    </div>
  );
};

const Composer: React.FC = () => {
  return (
    <ComposerPrimitive.Root className="flex gap-3 items-end max-w-4xl mx-auto">
      <ComposerPrimitive.Input
        rows={1}
        autoFocus
        placeholder="Write a message..."
        className="flex-1 min-h-12 max-h-48 px-4 py-3 text-base border rounded-xl bg-muted/50 border-border resize-none focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-all duration-200"
        data-testid="chat-input"
      />
      <ComposerAction />
    </ComposerPrimitive.Root>
  );
};

const ComposerAction: React.FC = () => {
  return (
    <>
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send asChild>
          <TooltipIconButton
            tooltip="Send message"
            variant="primary"
            className="h-12 w-12 shrink-0 rounded-xl"
            data-testid="send-button"
          >
            <SendHorizontalIcon size={18} />
          </TooltipIconButton>
        </ComposerPrimitive.Send>
      </ThreadPrimitive.If>
      <ThreadPrimitive.If running>
        <ComposerPrimitive.Cancel asChild>
          <TooltipIconButton
            tooltip="Stop generating"
            variant="primary"
            className="h-12 w-12 shrink-0 rounded-xl"
          >
            <div className="w-3 h-3 bg-current rounded-sm" />
          </TooltipIconButton>
        </ComposerPrimitive.Cancel>
      </ThreadPrimitive.If>
    </>
  );
};

const UserMessage: React.FC = () => {
  return (
    <MessagePrimitive.Root
      className="p-6 animate-in slide-in-from-bottom-4 duration-300"
      data-testid="user-message"
    >
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-end mb-2 h-5">
          <MessageTimestamp />
        </div>
        <div className="flex items-end gap-3 justify-end">
          <div
            className="max-w-[70%] p-4 bg-primary text-primary-foreground rounded-2xl rounded-br-md shadow-sm"
            data-testid="user-message-content"
          >
            <MessagePrimitive.Content />
          </div>
          <Avatar className="h-9 w-9 shrink-0">
            <AvatarFallback className="bg-primary text-primary-foreground">
              <UserIcon size={20} />
            </AvatarFallback>
          </Avatar>
        </div>
        <UserActionBar />
      </div>
      <BranchPicker className="justify-end pr-12" />
    </MessagePrimitive.Root>
  );
};

const UserActionBar: React.FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="flex gap-1 mt-2 justify-end pr-12 opacity-0 transition-opacity group-hover:opacity-100"
    >
      <ActionBarPrimitive.Edit asChild>
        <TooltipIconButton tooltip="Edit message" size="sm" variant="ghost">
          <PencilIcon size={14} />
        </TooltipIconButton>
      </ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
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

const AssistantMessage: React.FC = () => {
  const message = useMessage();

  // Check if message is loading by checking for our special marker
  // The assistant-ui library might not pass through our custom isLoading property
  const isLoading = message?.content?.[0]?.text === LOADING_MARKER;

  return (
    <MessagePrimitive.Root
      className="p-6 animate-in slide-in-from-bottom-4 duration-300 group"
      data-testid="assistant-message"
    >
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center mb-2 h-5">
          <MessageTimestamp />
        </div>
        <div className="flex items-start gap-3">
          <Avatar className="h-9 w-9 shrink-0">
            <AvatarFallback className="bg-muted border border-border">
              <BotIcon size={20} className="text-primary" />
            </AvatarFallback>
          </Avatar>
          <div
            className="max-w-[70%] p-4 bg-muted border rounded-2xl rounded-bl-md shadow-sm"
            data-testid="assistant-message-content"
          >
            {isLoading ? (
              <div className="flex items-center gap-1">
                <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce [animation-delay:-0.32s]"></div>
                <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce [animation-delay:-0.16s]"></div>
                <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce"></div>
              </div>
            ) : (
              <MessagePrimitive.Content components={messageContentComponents} />
            )}
          </div>
        </div>
        <AssistantActionBar />
      </div>
      <BranchPicker className="pl-12" />
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: React.FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      autohideFloat="single-branch"
      className="flex gap-1 mt-2 pl-12 opacity-0 transition-opacity group-hover:opacity-100"
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="Copy" size="sm" variant="ghost">
          <MessagePrimitive.If copied>
            <CheckIcon size={14} />
          </MessagePrimitive.If>
          <MessagePrimitive.If copied={false}>
            <CopyIcon size={14} />
          </MessagePrimitive.If>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton tooltip="Regenerate" size="sm" variant="ghost">
          <RefreshCwIcon size={14} />
        </TooltipIconButton>
      </ActionBarPrimitive.Reload>
    </ActionBarPrimitive.Root>
  );
};

const BranchPicker: React.FC<BranchPickerProps> = ({ className, ...rest }) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={classNames(
        'flex items-center gap-2 mt-2 text-sm text-muted-foreground',
        className
      )}
      {...rest}
    >
      <BranchPickerPrimitive.Previous asChild>
        <TooltipIconButton tooltip="Previous" size="sm" variant="ghost">
          <ChevronLeftIcon size={14} />
        </TooltipIconButton>
      </BranchPickerPrimitive.Previous>
      <Badge variant="secondary" className="text-xs font-mono">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </Badge>
      <BranchPickerPrimitive.Next asChild>
        <TooltipIconButton tooltip="Next" size="sm" variant="ghost">
          <ChevronRightIcon size={14} />
        </TooltipIconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};

const MessageTimestamp: React.FC = () => {
  // For now, showing relative time would require access to the message context
  // which isn't readily available in the current assistant-ui primitives structure
  // This would need to be passed down from the parent component in a real implementation
  return (
    <MessagePrimitive.If hasBranchPicker={false}>
      <time className="text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
        just now
      </time>
    </MessagePrimitive.If>
  );
};
