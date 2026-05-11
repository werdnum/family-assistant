import {
  ActionBarPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useComposer,
  useMessage,
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
import React, { createContext, useContext, useEffect, useState } from 'react';
import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from '@/components/assistant-ui/attachment';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { LOADING_MARKER } from './constants';
import { DynamicToolUI } from './DynamicToolUI';
import { MarkdownText } from './MarkdownText';
import { ToolGroup } from './ToolGroup';
import { TooltipIconButton } from './TooltipIconButton';

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

const Composer: React.FC = () => {
  const [attachmentError, setAttachmentError] = useState<string | null>(null);

  return (
    <ComposerPrimitive.Root className="flex flex-col gap-3 max-w-3xl mx-auto w-full">
      <ComposerAttachments />
      {attachmentError && (
        <p className="text-red-600 text-xs px-1" data-testid="attachment-error-message">
          {attachmentError}
        </p>
      )}
      <div className="flex gap-2 items-end">
        <ComposerAddAttachment
          onAttachmentAddStart={() => setAttachmentError(null)}
          onAttachmentAddError={setAttachmentError}
        />
        <div className="flex-1 relative">
          <ComposerPrimitive.Input
            rows={1}
            autoFocus
            placeholder="Message Family Assistant..."
            className="w-full min-h-11 max-h-48 pl-4 pr-4 py-2.5 text-sm border rounded-2xl bg-muted/40 border-border/60 resize-none focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-200 placeholder:text-muted-foreground/60"
            data-testid="chat-input"
          />
        </div>
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
          {/* @ts-expect-error - TooltipIconButton JSX component */}
          <TooltipIconButton
            tooltip={hasUploadingAttachments ? 'Uploading attachments...' : 'Send message'}
            variant="default"
            side="top"
            className="h-11 w-11 shrink-0 rounded-full"
            data-testid="send-button"
            disabled={hasUploadingAttachments}
          >
            {hasUploadingAttachments ? (
              <Loader2Icon size={16} className="animate-spin" />
            ) : (
              <ArrowUpIcon size={16} />
            )}
          </TooltipIconButton>
        </ComposerPrimitive.Send>
      </ThreadPrimitive.If>
      <ThreadPrimitive.If running>
        <ComposerPrimitive.Cancel asChild>
          {/* @ts-expect-error - TooltipIconButton JSX component */}
          <TooltipIconButton
            tooltip="Stop generating"
            variant="default"
            side="top"
            className="h-11 w-11 shrink-0 rounded-full"
          >
            <SquareIcon size={14} />
          </TooltipIconButton>
        </ComposerPrimitive.Cancel>
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
      className="flex items-center gap-1 mt-1 opacity-0 group-hover:opacity-100 transition-opacity duration-200"
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
