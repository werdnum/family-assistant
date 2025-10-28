import React, { useState, useEffect, createContext, useContext } from 'react';
import {
  ActionBarPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
  useComposer,
} from '@assistant-ui/react';
import {
  ArrowDownIcon,
  CheckIcon,
  CopyIcon,
  SendHorizontalIcon,
  UserIcon,
  BotIcon,
  Loader2Icon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { MarkdownText } from './MarkdownText';
import { TooltipIconButton } from './TooltipIconButton';
import { LOADING_MARKER } from './constants';
import { DynamicToolUI } from './DynamicToolUI';
import { ToolGroup } from './ToolGroup';
import {
  ComposerAttachments,
  ComposerAddAttachment,
  UserMessageAttachments,
} from '@/components/assistant-ui/attachment';

// API endpoints
const PROFILES_API_ENDPOINT = '/api/v1/profiles';

// Profile context for mapping profile IDs to descriptions
interface Profile {
  id: string;
  description: string;
}

interface ProfilesContextType {
  profiles: Record<string, Profile>;
  isLoading: boolean;
  error: string | null;
}

const ProfilesContext = createContext<ProfilesContextType>({
  profiles: {},
  isLoading: true,
  error: null,
});

const useProfiles = () => useContext(ProfilesContext);

const messageContentComponents = {
  Text: MarkdownText,
  ToolGroup, // ToolGroup should be at root level, not nested under tools
  tools: {
    // Use DynamicToolUI as the fallback which will handle all tools
    Fallback: DynamicToolUI,
    // Don't specify by_name since we want all tools to go through DynamicToolUI
  },
};

// ProfilesProvider component to fetch and provide profiles data
const ProfilesProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [profiles, setProfiles] = useState<Record<string, Profile>>({});
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        const response = await fetch(PROFILES_API_ENDPOINT);
        if (response.ok) {
          const data = await response.json();
          const profilesMap: Record<string, Profile> = {};
          data.profiles.forEach((profile: { id: string; description?: string }) => {
            profilesMap[profile.id] = {
              id: profile.id,
              description: profile.description || profile.id,
            };
          });
          setProfiles(profilesMap);
          setError(null);
        } else {
          setError(`Failed to fetch profiles: ${response.status}`);
        }
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : 'Unknown error';
        setError(`Error fetching profiles: ${errorMessage}`);
        console.error('Error fetching profiles:', error);
      } finally {
        setIsLoading(false);
      }
    };

    fetchProfiles();
  }, []);

  return (
    <ProfilesContext.Provider value={{ profiles, isLoading, error }}>
      {children}
    </ProfilesContext.Provider>
  );
};

export const Thread: React.FC = () => {
  return (
    <ProfilesProvider>
      <ThreadContent />
    </ProfilesProvider>
  );
};

const ThreadContent: React.FC = () => {
  return (
    <ThreadPrimitive.Root className="flex flex-1 flex-col bg-background min-h-0">
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-muted-foreground/20 min-h-0">
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

      <div className="flex-shrink-0 bg-background border-t p-4 md:p-6">
        <Composer />
      </div>
    </ThreadPrimitive.Root>
  );
};

const ThreadScrollToBottom: React.FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="absolute bottom-4 right-4 md:right-8 z-10 shadow-lg bg-background opacity-0 scale-75 transition-all duration-200 data-[enabled]:opacity-100 data-[enabled]:scale-100"
      >
        <ArrowDownIcon size={16} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: React.FC = () => {
  return (
    <ThreadPrimitive.Empty>
      <div className="flex min-h-[40vh] md:min-h-[60vh] flex-col items-center justify-center p-8 animate-in fade-in duration-300">
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
    <ComposerPrimitive.Root className="flex flex-col gap-3 max-w-4xl mx-auto">
      <ComposerAttachments />
      <div className="flex gap-3 items-end">
        <ComposerAddAttachment />
        <ComposerPrimitive.Input
          rows={1}
          autoFocus
          placeholder="Write a message..."
          className="flex-1 min-h-12 max-h-48 px-4 py-3 text-base border rounded-xl bg-muted/50 border-border resize-none focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-all duration-200"
          data-testid="chat-input"
        />
        <ComposerAction />
      </div>
    </ComposerPrimitive.Root>
  );
};

const ComposerAction: React.FC = () => {
  // Check if any attachments are currently uploading
  const hasUploadingAttachments = useComposer((state) => {
    const attachments = state.attachments || [];
    return attachments.some((att) => att.status?.type === 'running');
  });

  return (
    <>
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send asChild>
          <TooltipIconButton
            tooltip={hasUploadingAttachments ? 'Uploading attachments...' : 'Send message'}
            variant="default"
            side="top"
            className="h-12 w-12 shrink-0 rounded-xl"
            data-testid="send-button"
            disabled={hasUploadingAttachments}
          >
            {hasUploadingAttachments ? (
              <Loader2Icon size={18} className="animate-spin" />
            ) : (
              <SendHorizontalIcon size={18} />
            )}
          </TooltipIconButton>
        </ComposerPrimitive.Send>
      </ThreadPrimitive.If>
      <ThreadPrimitive.If running>
        <ComposerPrimitive.Cancel asChild>
          <TooltipIconButton
            tooltip="Stop generating"
            variant="default"
            side="top"
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
      className="p-6 animate-in slide-in-from-bottom-4 duration-300 group"
      data-testid="user-message"
    >
      <div className="max-w-4xl mx-auto relative">
        <div className="flex items-center justify-end mb-2 h-5">
          <MessageTimestamp />
        </div>
        <UserMessageAttachments />
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

const AssistantMessage: React.FC = () => {
  const message = useMessage();
  const { profiles, error } = useProfiles();

  // Check if message is loading by checking for our special marker
  // The assistant-ui library might not pass through our custom isLoading property
  const isLoading =
    Array.isArray(message?.content) &&
    message.content.length > 0 &&
    message.content[0]?.text === LOADING_MARKER;

  // Get profile info for this message
  const profileId = (message as { processing_profile_id?: string })?.processing_profile_id;
  const profile = profileId ? profiles[profileId] : null;

  return (
    <MessagePrimitive.Root
      className="p-6 animate-in slide-in-from-bottom-4 duration-300 group"
      data-testid="assistant-message"
    >
      <div className="max-w-4xl mx-auto relative">
        <div className="flex items-center justify-between mb-2 h-5">
          <MessageTimestamp />
          {profile && (
            <Badge
              variant="secondary"
              className="text-xs ml-2"
              title={`Generated by ${profile.description}`}
            >
              {profile.description}
            </Badge>
          )}
          {profileId && !profile && !error && (
            <Badge
              variant="outline"
              className="text-xs ml-2 opacity-50"
              title="Profile information loading..."
            >
              Loading...
            </Badge>
          )}
          {profileId && !profile && error && (
            <Badge
              variant="destructive"
              className="text-xs ml-2 opacity-70"
              title={`Profile unavailable: ${error}`}
            >
              Profile Error
            </Badge>
          )}
        </div>
        <div className="flex items-start gap-3">
          <Avatar className="h-9 w-9 shrink-0">
            <AvatarFallback className="bg-muted border border-border">
              <BotIcon size={20} className="text-primary" />
            </AvatarFallback>
          </Avatar>
          <div className="flex-1">
            <div className="relative inline-block max-w-[70%]">
              <div
                className="p-4 bg-muted border rounded-2xl rounded-bl-md shadow-sm"
                data-testid="assistant-message-content"
              >
                {isLoading ? (
                  <div className="flex items-center gap-1">
                    <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce [animation-delay:-0.32s]"></div>
                    <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce [animation-delay:-0.16s]"></div>
                    <div className="w-2 h-2 bg-muted-foreground/50 rounded-full animate-bounce"></div>
                  </div>
                ) : (
                  <>
                    {Array.isArray(message.content) ? (
                      <MessagePrimitive.Content components={messageContentComponents} />
                    ) : typeof message.content === 'string' ? (
                      <MarkdownText>{message.content}</MarkdownText>
                    ) : message.content ? (
                      <MarkdownText>{String(message.content)}</MarkdownText>
                    ) : (
                      <div className="text-muted-foreground italic">No content</div>
                    )}
                  </>
                )}
              </div>
              <AssistantActionBar />
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
      className="absolute top-2 -right-9 flex items-start opacity-0 group-hover:opacity-100 transition-opacity duration-200"
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
    </ActionBarPrimitive.Root>
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
