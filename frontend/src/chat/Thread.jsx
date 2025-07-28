import React from 'react';
import {
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
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
  Loader2Icon,
} from 'lucide-react';
import classNames from 'classnames';
// import { formatRelativeTime } from './utils';
import { MarkdownText } from './MarkdownText';
import { TooltipIconButton } from './TooltipIconButton';

export const Thread = () => {
  return (
    <ThreadPrimitive.Root className="thread-root">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadWelcome />

        <ThreadPrimitive.Messages
          components={{
            UserMessage: UserMessage,
            EditComposer: EditComposer,
            AssistantMessage: AssistantMessage,
          }}
        />

        <ThreadPrimitive.If empty={false}>
          <div className="thread-spacer" />
        </ThreadPrimitive.If>

        <div className="thread-footer">
          <ThreadScrollToBottom />
          <Composer />
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadScrollToBottom = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="scroll-to-bottom-btn"
      >
        <ArrowDownIcon size={16} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome = () => {
  return (
    <ThreadPrimitive.Empty>
      <div className="thread-welcome">
        <div className="welcome-content">
          <div className="welcome-icon">
            <BotIcon size={48} strokeWidth={1.5} />
          </div>
          <h3 className="welcome-title">Welcome to Family Assistant</h3>
          <p className="welcome-subtitle">How can I help you today?</p>
        </div>
        <ThreadWelcomeSuggestions />
      </div>
    </ThreadPrimitive.Empty>
  );
};

const ThreadWelcomeSuggestions = () => {
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
    <div className="welcome-suggestions">
      {suggestions.map((suggestion, index) => (
        <ThreadPrimitive.Suggestion
          key={index}
          className="suggestion-card"
          prompt={suggestion.prompt}
          method="replace"
          autoSend
        >
          <span className="suggestion-icon">{suggestion.icon}</span>
          <span className="suggestion-text">{suggestion.prompt}</span>
        </ThreadPrimitive.Suggestion>
      ))}
    </div>
  );
};

const Composer = () => {
  return (
    <ComposerPrimitive.Root className="composer-root">
      <ComposerPrimitive.Input
        rows={1}
        autoFocus
        placeholder="Write a message..."
        className="composer-input"
        data-testid="chat-input"
      />
      <ComposerAction />
    </ComposerPrimitive.Root>
  );
};

const ComposerAction = () => {
  return (
    <>
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send asChild>
          <TooltipIconButton
            tooltip="Send message"
            variant="primary"
            className="composer-send"
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
            className="composer-cancel"
          >
            <div className="stop-icon" />
          </TooltipIconButton>
        </ComposerPrimitive.Cancel>
      </ThreadPrimitive.If>
    </>
  );
};

const UserMessage = () => {
  return (
    <MessagePrimitive.Root className="message-root user-message" data-testid="user-message">
      <div className="message-container">
        <div className="message-header">
          <MessageTimestamp />
        </div>
        <div className="message-content-wrapper">
          <div className="message-bubble user-bubble" data-testid="user-message-content">
            <MessagePrimitive.Content components={{ Text: 'span' }} />
          </div>
          <div className="message-avatar user-avatar">
            <UserIcon size={20} />
          </div>
        </div>
        <UserActionBar />
      </div>
      <BranchPicker className="branch-picker-user" />
    </MessagePrimitive.Root>
  );
};

const UserActionBar = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="action-bar user-action-bar"
    >
      <ActionBarPrimitive.Edit asChild>
        <TooltipIconButton tooltip="Edit message" size="sm">
          <PencilIcon size={14} />
        </TooltipIconButton>
      </ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  );
};

const EditComposer = () => {
  return (
    <ComposerPrimitive.Root className="edit-composer">
      <ComposerPrimitive.Input className="edit-composer-input" autoFocus />
      <div className="edit-composer-actions">
        <ComposerPrimitive.Cancel asChild>
          <button className="edit-cancel-btn">Cancel</button>
        </ComposerPrimitive.Cancel>
        <ComposerPrimitive.Send asChild>
          <button className="edit-send-btn">Save</button>
        </ComposerPrimitive.Send>
      </div>
    </ComposerPrimitive.Root>
  );
};

const AssistantMessage = () => {
  return (
    <MessagePrimitive.Root
      className="message-root assistant-message"
      data-testid="assistant-message"
    >
      <div className="message-container">
        <div className="message-header">
          <MessageTimestamp />
        </div>
        <div className="message-content-wrapper">
          <div className="message-avatar assistant-avatar">
            <BotIcon size={20} />
          </div>
          <div className="message-bubble assistant-bubble" data-testid="assistant-message-content">
            <MessagePrimitive.Content components={{ Text: MarkdownText }} />
          </div>
        </div>
        <AssistantActionBar />
      </div>
      <BranchPicker className="branch-picker-assistant" />
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      autohideFloat="single-branch"
      className="action-bar assistant-action-bar"
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="Copy" size="sm">
          <MessagePrimitive.If copied>
            <CheckIcon size={14} />
          </MessagePrimitive.If>
          <MessagePrimitive.If copied={false}>
            <CopyIcon size={14} />
          </MessagePrimitive.If>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton tooltip="Regenerate" size="sm">
          <RefreshCwIcon size={14} />
        </TooltipIconButton>
      </ActionBarPrimitive.Reload>
    </ActionBarPrimitive.Root>
  );
};

const BranchPicker = ({ className, ...rest }) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={classNames('branch-picker', className)}
      {...rest}
    >
      <BranchPickerPrimitive.Previous asChild>
        <TooltipIconButton tooltip="Previous" size="sm">
          <ChevronLeftIcon size={14} />
        </TooltipIconButton>
      </BranchPickerPrimitive.Previous>
      <span className="branch-picker-count">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <TooltipIconButton tooltip="Next" size="sm">
          <ChevronRightIcon size={14} />
        </TooltipIconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};

const MessageTimestamp = () => {
  // For now, showing relative time would require access to the message context
  // which isn't readily available in the current assistant-ui primitives structure
  // This would need to be passed down from the parent component in a real implementation
  return (
    <MessagePrimitive.If hasBranchPicker={false}>
      <time className="message-timestamp">just now</time>
    </MessagePrimitive.If>
  );
};

// Loading indicator component
export const ThreadLoading = () => {
  return (
    <div className="thread-loading">
      <div className="loading-message">
        <div className="message-avatar assistant-avatar">
          <BotIcon size={20} />
        </div>
        <div className="loading-bubble">
          <Loader2Icon size={16} className="loading-spinner" />
          <span>Thinking...</span>
        </div>
      </div>
    </div>
  );
};
