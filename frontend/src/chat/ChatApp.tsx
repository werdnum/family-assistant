import React, { useState, useCallback, useEffect, useRef } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { Menu } from 'lucide-react';
import { Thread } from './Thread';
import ConversationSidebar from './ConversationSidebar';
import { useStreamingResponse } from './useStreamingResponse';
import { LOADING_MARKER } from './constants';
import { generateUUID } from '../utils/uuid';
import { defaultAttachmentAdapter } from './attachmentAdapter';
import { ChatAppProps, Message, MessageContent, Conversation } from './types';
import NavigationSheet from '../shared/NavigationSheet';
import { ToolConfirmationProvider } from './ToolConfirmationContext';
import ProfileSelector from './ProfileSelector';

// Helper function to parse tool arguments
const parseToolArguments = (args: unknown): Record<string, unknown> => {
  if (typeof args === 'string') {
    try {
      return JSON.parse(args);
    } catch (e) {
      console.error('Failed to parse tool arguments:', e);
      return { raw: args };
    }
  }
  return args;
};

const ChatApp: React.FC<ChatAppProps> = ({ profileId = 'default_assistant' }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState<boolean>(true);
  const [isMobile, setIsMobile] = useState<boolean>(window.innerWidth <= 768);
  const [currentProfileId, setCurrentProfileId] = useState<string>(() => {
    // Load saved profile from localStorage, fallback to prop
    return localStorage.getItem('selectedProfileId') || profileId;
  });
  const streamingMessageIdRef = useRef<string | null>(null);
  const toolCallMessageIdRef = useRef<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesAbortControllerRef = useRef<AbortController | null>(null);

  // Fetch conversations list
  const fetchConversations = useCallback(async () => {
    try {
      // Cancel previous request if it exists
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      // Create new abort controller
      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      setConversationsLoading(true);
      const response = await fetch('/api/v1/chat/conversations?interface_type=web', {
        signal: abortController.signal,
      });
      if (response.ok) {
        const data = await response.json();
        setConversations(data.conversations);
      }
    } catch (error) {
      // Don't log error if request was aborted (component unmounting)
      if (error instanceof Error && error.name !== 'AbortError') {
        console.error('Error fetching conversations:', error);
      }
    } finally {
      setConversationsLoading(false);
    }
  }, []);

  // Track pending confirmations by tool call ID
  const [pendingConfirmations, setPendingConfirmations] = useState<
    Map<string, { request_id: string; [key: string]: unknown }>
  >(new Map());

  const handleConfirmationRequest = useCallback(
    (request: { tool_call_id: string; request_id: string; [key: string]: unknown }) => {
      // Add to pending confirmations map
      setPendingConfirmations((prev) => {
        const newMap = new Map(prev);
        // Store by tool_call_id for matching
        newMap.set(request.tool_call_id, request);
        return newMap;
      });
    },
    []
  );

  const handleConfirmationResult = useCallback(
    (result: { request_id: string; [key: string]: unknown }) => {
      // Remove from pending confirmations
      setPendingConfirmations((prev) => {
        const newMap = new Map(prev);
        // Find and remove the confirmation by matching request_id
        for (const [key, value] of newMap.entries()) {
          if (value.request_id === result.request_id) {
            newMap.delete(key);
            break;
          }
        }
        return newMap;
      });
    },
    []
  );

  const handleConfirmation = useCallback(
    async (toolCallId: string, requestId: string, approved: boolean) => {
      try {
        const response = await fetch('/api/v1/chat/confirm_tool', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            request_id: requestId,
            approved: approved,
            conversation_id: conversationId,
          }),
        });

        if (!response.ok) {
          console.error('Failed to send confirmation:', response.status);
        }
      } catch (error) {
        console.error('Error sending confirmation:', error);
      }
    },
    [conversationId]
  );

  // Streaming callbacks
  const handleStreamingMessage = useCallback((content: string) => {
    if (streamingMessageIdRef.current) {
      setMessages((prev) => {
        // Update the loading message with actual content
        return prev.map((msg) => {
          if (msg.id === streamingMessageIdRef.current) {
            // Preserve existing tool calls when updating text
            const existingContent = msg.content || [];
            const toolCalls = existingContent.filter((part) => part.type === 'tool-call');

            // Create new content array with updated text and preserved tool calls
            const newContent: MessageContent[] = [
              {
                type: 'text',
                text: content, // Use the accumulated content directly from the hook
              },
              ...toolCalls, // Preserve any existing tool calls with their status and results
            ];

            return {
              ...msg,
              content: newContent,
              isLoading: false, // Remove loading flag when content arrives
            };
          }
          return msg;
        });
      });
    }
  }, []);

  const handleStreamingError = useCallback((error: Error | string, _metadata: unknown) => {
    console.error('Streaming error:', error, _metadata);
    if (streamingMessageIdRef.current) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === streamingMessageIdRef.current
            ? {
                ...msg,
                content: [
                  { type: 'text', text: 'Sorry, I encountered an error processing your message.' },
                ],
                isLoading: false, // Remove loading state on error
              }
            : msg
        )
      );
      streamingMessageIdRef.current = null;
    }
  }, []);

  const handleStreamingComplete = useCallback(
    ({
      content,
      toolCalls: _toolCalls,
    }: {
      content: string;
      toolCalls: Array<Record<string, unknown>>;
    }) => {
      // Capture ref values locally to avoid race conditions
      const messageId = streamingMessageIdRef.current;
      const toolCallMessageId = toolCallMessageIdRef.current;

      if (messageId && content) {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id === messageId) {
              // Preserve any existing tool calls
              const existingToolCalls =
                msg.content?.filter((part) => part.type === 'tool-call') || [];
              return {
                ...msg,
                content: [{ type: 'text', text: content }, ...existingToolCalls],
                status: 'done' as const,
                isLoading: false, // Ensure loading state is cleared
              };
            }
            return msg;
          })
        );

        // Clean up the references immediately after state update
        // Only clear if the IDs haven't changed (avoiding race with new streaming)
        if (streamingMessageIdRef.current === messageId) {
          streamingMessageIdRef.current = null;
        }
        if (toolCallMessageIdRef.current === toolCallMessageId) {
          toolCallMessageIdRef.current = null;
        }

        // Refresh conversations after state is updated
        fetchConversations();
      }

      // Update tool call message status when streaming completes
      if (toolCallMessageId) {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id === toolCallMessageId) {
              // Check if all tool calls have results
              const allToolsComplete = msg.content?.every(
                (part) => part.type !== 'tool-call' || part.result !== undefined
              );

              return {
                ...msg,
                status: allToolsComplete ? { type: 'complete' } : msg.status,
              };
            }
            return msg;
          })
        );
      }
    },
    [fetchConversations]
  );

  // Handle tool calls during streaming
  const handleStreamingToolCall = useCallback((toolCalls: Array<Record<string, unknown>>) => {
    // CRITICAL FIX: Capture the ref value immediately to avoid closure issues
    // This prevents race conditions where the ref gets cleared before setState callback executes
    const targetMessageId = streamingMessageIdRef.current;

    console.log(
      `[ATTACH-STATE] handleStreamingToolCall called | toolCallCount=${toolCalls?.length || 0} | messageId=${targetMessageId} | ts=${Date.now()}`
    );

    // Check for attach_to_response specifically
    const attachToolCalls = toolCalls?.filter((tc) => tc.name === 'attach_to_response') || [];
    if (attachToolCalls.length > 0) {
      console.log(
        `[ATTACH-STATE] Found attach_to_response | count=${attachToolCalls.length} | hasAttachments=${attachToolCalls.some((tc) => tc.attachments)} | ts=${Date.now()}`
      );
    }

    if (toolCalls && toolCalls.length > 0 && targetMessageId) {
      setMessages((prev) => {
        console.log(
          `[ATTACH-STATE] setMessages called | prevMessageCount=${prev.length} | targetMessageId=${targetMessageId} | ts=${Date.now()}`
        );

        const updatedMessages = prev.map((msg) => {
          if (msg.id === targetMessageId) {
            console.log(
              `[ATTACH-STATE] Updating message | messageId=${msg.id} | existingContentLength=${msg.content?.length || 0} | newToolCount=${toolCalls.length} | ts=${Date.now()}`
            );

            // This is the message to update.
            // It might be a 'loading' message, or it might already have text.
            toolCallMessageIdRef.current = msg.id;

            const existingTextContent =
              msg.content?.filter((part) => part.type === 'text' && part.text !== LOADING_MARKER) ||
              [];

            // Create new tool parts with new object references
            const toolParts: MessageContent[] = toolCalls.map((tc) => {
              const args = parseToolArguments(tc.arguments);
              return {
                type: 'tool-call',
                toolCallId: tc.id as string,
                toolName: tc.name as string,
                args: args,
                argsText:
                  typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments),
                // Ensure result and attachments are new objects if present
                ...(tc.result && { result: tc.result as string }),
                ...(tc.attachments && {
                  attachments: Array.isArray(tc.attachments) ? [...tc.attachments] : tc.attachments,
                }),
              };
            });

            // Log the new message structure
            const newMsg = {
              ...msg,
              content: [...existingTextContent, ...toolParts],
              isLoading: false,
              status: { type: 'running' },
              // Add a key that changes when tool results arrive to force re-render
              _updateKey: Date.now(),
            };
            console.log(
              `[ATTACH-STATE] Message updated | messageId=${newMsg.id} | contentTypes=${newMsg.content?.map((c) => c.type).join(',')} | ts=${Date.now()}`
            );

            // Return completely new message object
            return newMsg;
          }
          return msg;
        });

        console.log(
          `[ATTACH-STATE] setMessages complete | updatedCount=${updatedMessages.filter((m) => m.id === targetMessageId).length} | ts=${Date.now()}`
        );
        return updatedMessages;
      });
    }
  }, []);

  // Initialize streaming hook
  const { sendStreamingMessage, cancelStream, isStreaming } = useStreamingResponse({
    onMessage: handleStreamingMessage,
    onError: handleStreamingError,
    onComplete: handleStreamingComplete,
    onToolCall: handleStreamingToolCall,
    onToolConfirmationRequest: handleConfirmationRequest,
    onToolConfirmationResult: handleConfirmationResult,
  });

  // Cleanup effect to abort fetch requests on unmount
  useEffect(() => {
    return () => {
      // Cancel any pending fetch requests when component unmounts
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }
    };
  }, []);

  // Handle window resize
  useEffect(() => {
    const handleResize = () => {
      const newIsMobile = window.innerWidth <= 768;
      setIsMobile(newIsMobile);

      // Close sidebar when switching to mobile to prevent layout issues
      if (newIsMobile) {
        setSidebarOpen(false);
      }
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Initialize conversation ID from URL or localStorage
  useEffect(() => {
    fetchConversations();

    const urlParams = new URLSearchParams(window.location.search);
    const urlConversationId = urlParams.get('conversation_id');
    const lastConversationId = localStorage.getItem('lastConversationId');

    if (urlConversationId) {
      setConversationId(urlConversationId);
      loadConversationMessages(urlConversationId);
    } else if (lastConversationId) {
      setConversationId(lastConversationId);
      loadConversationMessages(lastConversationId);
      window.history.replaceState({}, '', `/chat?conversation_id=${lastConversationId}`);
    } else {
      handleNewChat();
    }
  }, []);

  // Load messages for a conversation
  const loadConversationMessages = async (convId: string) => {
    try {
      // Cancel previous messages request if it exists
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }

      // Create new abort controller for messages
      const messagesAbortController = new AbortController();
      messagesAbortControllerRef.current = messagesAbortController;

      setIsLoading(true);
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages`, {
        signal: messagesAbortController.signal,
      });
      if (response.ok) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const data: { messages: any[] } = await response.json();

        const processedMessages: Message[] = [];
        const toolResponses = new Map<string, string>();

        data.messages.forEach((msg) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            toolResponses.set(msg.tool_call_id, msg.content || 'Tool executed successfully');
          }
        });

        data.messages.forEach((msg) => {
          if (msg.role === 'tool') {
            return;
          }

          if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
            const content: MessageContent[] = [];
            if (msg.content) {
              // Handle content - filter out image_url if present
              if (typeof msg.content === 'string') {
                content.push({ type: 'text', text: msg.content });
              } else if (Array.isArray(msg.content)) {
                for (const part of msg.content) {
                  if (part.type === 'text') {
                    content.push({ type: 'text', text: part.text });
                  }
                  // Skip image_url content types
                }
              }
            }

            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            msg.tool_calls.forEach((toolCall: any) => {
              const toolResponse = toolResponses.get(toolCall.id);
              // Extract the function name from the tool call
              const toolName = toolCall.function?.name || toolCall.name || 'unknown';
              // Parse arguments if they're a string
              const args = parseToolArguments(toolCall.function?.arguments || toolCall.arguments);

              content.push({
                type: 'tool-call',
                toolCallId: toolCall.id,
                toolName: toolName,
                args: args,
                argsText:
                  typeof toolCall.function?.arguments === 'string'
                    ? toolCall.function.arguments
                    : typeof toolCall.arguments === 'string'
                      ? toolCall.arguments
                      : JSON.stringify(args),
                result: toolResponse ?? undefined,
              });
            });

            processedMessages.push({
              id: `msg_${msg.internal_id}`,
              role: 'assistant',
              content: content,
              createdAt: new Date(msg.timestamp),
            });
            return;
          }

          // For user messages with potential attachments
          const messageContent: MessageContent[] = [];
          const attachments: Message['attachments'] = [];

          // Handle content - if it's a string, it's just text
          if (msg.content) {
            if (typeof msg.content === 'string') {
              messageContent.push({ type: 'text', text: msg.content });
            } else if (Array.isArray(msg.content)) {
              // If content is an array, process both text and image_url types
              for (const part of msg.content) {
                if (part.type === 'text') {
                  messageContent.push({ type: 'text', text: part.text });
                } else if (part.type === 'image_url' && part.image_url) {
                  // Convert image_url to attachment for display
                  attachments.push({
                    id: `att_${msg.internal_id}_${attachments.length}`,
                    type: 'image',
                    name: `Image ${attachments.length + 1}`,
                    content: part.image_url.url,
                  });
                }
              }
            }
          }

          // Handle attachments from the dedicated attachments field (new format)
          if (msg.attachments && Array.isArray(msg.attachments)) {
            for (const attachment of msg.attachments) {
              attachments.push({
                id: `att_${msg.internal_id}_${attachments.length}`,
                type: attachment.type || 'file',
                name: attachment.name || `Attachment ${attachments.length + 1}`,
                content: attachment.content_url,
              });
            }
          }

          processedMessages.push({
            id: `msg_${msg.internal_id}`,
            role: msg.role,
            content: messageContent.length > 0 ? messageContent : [{ type: 'text', text: '' }],
            createdAt: new Date(msg.timestamp),
            attachments: attachments.length > 0 ? attachments : undefined,
          });
        });

        // Ensure all messages have content as arrays before setting
        const messagesWithArrayContent = processedMessages.map((msg) => ({
          ...msg,
          content: Array.isArray(msg.content) ? msg.content : msg.content ? [msg.content] : [],
        }));

        setMessages(messagesWithArrayContent);
      }
    } catch (error) {
      // Don't log error if request was aborted (component unmounting)
      if (error instanceof Error && error.name !== 'AbortError') {
        console.error('Error loading conversation:', error);
      }
    } finally {
      setIsLoading(false);
    }
  };

  // Handle conversation selection
  const handleConversationSelect = (convId: string) => {
    // Cancel any active streaming before switching conversations
    cancelStream();

    setConversationId(convId);
    localStorage.setItem('lastConversationId', convId);
    window.history.pushState({}, '', `/chat?conversation_id=${convId}`);
    loadConversationMessages(convId);

    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };

  // Handle new chat creation
  const handleNewChat = useCallback(() => {
    // Cancel any active streaming before creating a new chat
    cancelStream();

    const newConvId = `web_conv_${generateUUID()}`;
    setConversationId(newConvId);
    setMessages([]);
    localStorage.setItem('lastConversationId', newConvId);
    window.history.pushState({}, '', `/chat?conversation_id=${newConvId}`);

    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  }, [cancelStream]);

  // Handle profile changes
  const handleProfileChange = useCallback(
    (newProfileId: string) => {
      setCurrentProfileId(newProfileId);
      // Persist selection to localStorage
      localStorage.setItem('selectedProfileId', newProfileId);

      // Optionally start a new conversation when switching profiles
      // to maintain clear context separation
      if (currentProfileId !== newProfileId && conversationId) {
        handleNewChat();
      }
    },
    [currentProfileId, conversationId, handleNewChat]
  );

  // Handle new messages from the user
  const handleNew = useCallback(
    async (message: {
      content: { text: string }[];
      attachments?: Array<{
        id?: string;
        type?: string;
        name: string;
        content?: string;
        file?: File;
      }>;
    }) => {
      // Process attachments - they might come from the runtime with different properties
      const processedAttachments = message.attachments?.map((att) => ({
        id: att.id || `att_${Date.now()}_${Math.random()}`,
        type: (att.type || 'image') as 'image',
        name: att.name,
        content: att.content || '', // Content might be base64 or empty if still processing
      }));

      const userMessage: Message = {
        id: `msg_${Date.now()}`,
        role: 'user',
        content: message.content,
        createdAt: new Date(),
        attachments: processedAttachments,
      };

      const assistantMessageId = `msg_${Date.now()}_assistant`;
      const loadingAssistantMessage: Message = {
        id: assistantMessageId,
        role: 'assistant',
        content: [{ type: 'text', text: LOADING_MARKER }],
        isLoading: true,
        createdAt: new Date(),
      };

      setMessages((prev) => [...prev, userMessage, loadingAssistantMessage]);

      streamingMessageIdRef.current = assistantMessageId;

      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: conversationId || `web_conv_${generateUUID()}`,
        profileId: currentProfileId,
        interfaceType: 'web',
        attachments: processedAttachments,
      });
    },
    [conversationId, sendStreamingMessage, currentProfileId]
  );

  const convertMessage = useCallback((message: Message) => {
    // Ensure content is always an array for assistant-ui compatibility
    const converted = {
      ...message,
      content: Array.isArray(message.content)
        ? message.content
        : message.content
          ? [message.content]
          : [{ type: 'text', text: '' }],
      // Pass through attachments if they exist
      attachments: message.attachments,
    };

    // Ensure each content item has the right structure
    if (Array.isArray(converted.content)) {
      converted.content = converted.content.filter((item) => item && typeof item === 'object');
      if (converted.content.length === 0) {
        converted.content = [{ type: 'text', text: '' }];
      }
    }

    return converted;
  }, []);

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || isStreaming,
    onNew: handleNew,
    convertMessage,
    adapters: {
      attachments: defaultAttachmentAdapter,
    },
  });

  return (
    <div className="flex h-screen flex-col bg-background">
      {/* Header */}
      <div className="sticky top-0 z-50 flex items-center gap-4 border-b bg-background/95 p-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label="Toggle sidebar"
        >
          <Menu className="h-4 w-4" />
        </Button>
        <h2 className="text-xl font-semibold">Chat</h2>

        {/* Profile Selector */}
        <div className="flex items-center">
          <ProfileSelector
            selectedProfileId={currentProfileId}
            onProfileChange={handleProfileChange}
            disabled={isLoading}
          />
        </div>

        {/* Main Navigation Menu */}
        <div className="ml-auto">
          <NavigationSheet currentPage="chat">
            <Button variant="outline" size="sm">
              <Menu className="h-4 w-4" />
              <span className="sr-only">Open main menu</span>
            </Button>
          </NavigationSheet>
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar - Desktop */}
        {!isMobile && (
          <ConversationSidebar
            conversations={conversations}
            conversationsLoading={conversationsLoading}
            currentConversationId={conversationId}
            onConversationSelect={handleConversationSelect}
            onNewChat={handleNewChat}
            isOpen={sidebarOpen}
            onRefresh={fetchConversations}
            isMobile={isMobile}
          />
        )}

        {/* Sidebar - Mobile Sheet (Portal-based overlay) */}
        <Sheet open={sidebarOpen && isMobile} onOpenChange={setSidebarOpen}>
          <SheetContent side="left" className="w-80 p-0">
            <ConversationSidebar
              conversations={conversations}
              conversationsLoading={conversationsLoading}
              currentConversationId={conversationId}
              onConversationSelect={handleConversationSelect}
              onNewChat={handleNewChat}
              isOpen={true}
              onRefresh={fetchConversations}
              isMobile={isMobile}
            />
          </SheetContent>
        </Sheet>

        {/* Main content */}
        <div className="flex min-w-0 flex-1 flex-col">
          <main className="flex flex-1 flex-col min-h-0">
            <AssistantRuntimeProvider runtime={runtime}>
              <ToolConfirmationProvider value={{ pendingConfirmations, handleConfirmation }}>
                <div className="flex flex-1 flex-col min-h-0">
                  <div className="border-b bg-muted/50 p-6 flex-shrink-0">
                    <h2 className="text-xl font-semibold">Family Assistant Chat</h2>
                    {conversationId && (
                      <div className="mt-1 text-xs text-muted-foreground font-mono">
                        Conversation: {conversationId.substring(0, 20)}...
                      </div>
                    )}
                  </div>
                  <Thread />
                </div>
              </ToolConfirmationProvider>
            </AssistantRuntimeProvider>
          </main>
          <footer className="hidden md:block border-t p-4 text-center text-sm text-muted-foreground bg-background">
            <p>&copy; {new Date().getFullYear()} Family Assistant</p>
          </footer>
        </div>
      </div>
    </div>
  );
};

export default ChatApp;
