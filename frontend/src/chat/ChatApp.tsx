import React, { useState, useCallback, useEffect, useRef } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { Thread } from './Thread';
import NavHeader from './NavHeader';
import ConversationSidebar from './ConversationSidebar';
import { useStreamingResponse } from './useStreamingResponse';
import { LOADING_MARKER } from './constants';
import { generateUUID } from '../utils/uuid';
import { ChatAppProps, Message, MessageContent, Conversation } from './types';

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
  const streamingMessageIdRef = useRef<string | null>(null);
  const toolCallMessageIdRef = useRef<string | null>(null);

  // Fetch conversations list
  const fetchConversations = useCallback(async () => {
    try {
      setConversationsLoading(true);
      const response = await fetch('/api/v1/chat/conversations');
      if (response.ok) {
        const data = await response.json();
        setConversations(data.conversations);
      }
    } catch (error) {
      console.error('Error fetching conversations:', error);
    } finally {
      setConversationsLoading(false);
    }
  }, []);

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

  const handleStreamingComplete = useCallback(() => {
    // No need to do anything here - messages are already properly set up
    // Just clean up the references
    fetchConversations();
    streamingMessageIdRef.current = null;
    toolCallMessageIdRef.current = null;
  }, [fetchConversations]);

  // Handle tool calls during streaming
  const handleStreamingToolCall = useCallback((toolCalls: Array<Record<string, unknown>>) => {
    if (toolCalls && toolCalls.length > 0) {
      setMessages((prev) => {
        let updatedMessages = [...prev];

        // If this is the first tool call, convert the loading message to a tool call message
        if (!toolCallMessageIdRef.current && streamingMessageIdRef.current) {
          updatedMessages = updatedMessages.map((msg) => {
            if (msg.id === streamingMessageIdRef.current) {
              // Store this as the tool call message ID
              toolCallMessageIdRef.current = msg.id;

              // Replace the loading message with tool calls
              const toolParts: MessageContent[] = toolCalls.map((tc) => {
                const args = parseToolArguments(tc.arguments);
                return {
                  type: 'tool-call',
                  toolCallId: tc.id,
                  toolName: tc.name,
                  args: args,
                  argsText:
                    typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments),
                  ...(tc.result && { result: tc.result }),
                  // Note: status is on the message level, not on individual tool calls
                };
              });

              return {
                ...msg,
                content: toolParts,
                isLoading: false,
                // Set message status to indicate tool calls are running
                status: {
                  type: 'running',
                },
              };
            }
            return msg;
          });

          // Add a new loading message for the text response
          const textResponseMessageId = `${streamingMessageIdRef.current}_text`;
          updatedMessages.push({
            id: textResponseMessageId,
            role: 'assistant',
            content: [{ type: 'text', text: LOADING_MARKER }],
            isLoading: true,
            createdAt: new Date(),
          });

          // Update the streaming message ID to point to the text message
          streamingMessageIdRef.current = textResponseMessageId;
        } else if (toolCallMessageIdRef.current) {
          // Update existing tool call message with new/updated tool calls
          updatedMessages = updatedMessages.map((msg) => {
            if (msg.id === toolCallMessageIdRef.current) {
              const toolParts: MessageContent[] = toolCalls.map((tc) => {
                const args = parseToolArguments(tc.arguments);
                return {
                  type: 'tool-call',
                  toolCallId: tc.id,
                  toolName: tc.name,
                  args: args,
                  argsText:
                    typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments),
                  ...(tc.result && { result: tc.result }),
                  // Note: status is on the message level, not on individual tool calls
                };
              });

              // Check if all tool calls have results
              const allToolsComplete = toolParts.every((tc) => tc.result !== undefined);

              return {
                ...msg,
                content: toolParts,
                // Update status based on whether all tools are complete
                status: allToolsComplete ? { type: 'complete' } : { type: 'running' },
              };
            }
            return msg;
          });
        }

        return updatedMessages;
      });
    }
  }, []);

  // Initialize streaming hook
  const { sendStreamingMessage, isStreaming } = useStreamingResponse({
    onMessage: handleStreamingMessage,
    onError: handleStreamingError,
    onComplete: handleStreamingComplete,
    onToolCall: handleStreamingToolCall,
  });

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
      setIsLoading(true);
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages`);
      if (response.ok) {
        const data = await response.json();

        const processedMessages: Message[] = [];
        const toolResponses = new Map<string, string>();

        data.messages.forEach((msg: Record<string, any>) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            toolResponses.set(msg.tool_call_id, msg.content || 'Tool executed successfully');
          }
        });

        data.messages.forEach((msg: Record<string, any>) => {
          if (msg.role === 'tool') {
            return;
          }

          if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
            const content: MessageContent[] = [];
            if (msg.content) {
              content.push({ type: 'text', text: msg.content });
            }

            msg.tool_calls.forEach((toolCall: Record<string, any>) => {
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

          processedMessages.push({
            id: `msg_${msg.internal_id}`,
            role: msg.role,
            content: msg.content ? [{ type: 'text', text: msg.content }] : [],
            createdAt: new Date(msg.timestamp),
          });
        });

        setMessages(processedMessages);
      }
    } catch (error) {
      console.error('Error loading conversation:', error);
    } finally {
      setIsLoading(false);
    }
  };

  // Handle conversation selection
  const handleConversationSelect = (convId: string) => {
    setConversationId(convId);
    localStorage.setItem('lastConversationId', convId);
    window.history.pushState({}, '', `/chat?conversation_id=${convId}`);
    loadConversationMessages(convId);

    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };

  // Handle new chat creation
  const handleNewChat = () => {
    const newConvId = `web_conv_${generateUUID()}`;
    setConversationId(newConvId);
    setMessages([]);
    localStorage.setItem('lastConversationId', newConvId);
    window.history.pushState({}, '', `/chat?conversation_id=${newConvId}`);

    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };

  // Handle new messages from the user
  const handleNew = useCallback(
    async (message: { content: { text: string }[] }) => {
      const userMessage: Message = {
        id: `msg_${Date.now()}`,
        role: 'user',
        content: [{ type: 'text', text: message.content[0].text }],
        createdAt: new Date(),
      };

      const assistantMessageId = `msg_${Date.now()}_assistant`;
      // Add both user message and a loading assistant message
      const loadingAssistantMessage: Message = {
        id: assistantMessageId,
        role: 'assistant',
        content: [{ type: 'text', text: LOADING_MARKER }], // Special marker for loading state
        isLoading: true, // Custom flag to indicate loading state
        createdAt: new Date(),
      };

      setMessages((prev) => [...prev, userMessage, loadingAssistantMessage]);

      streamingMessageIdRef.current = assistantMessageId;

      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: conversationId || `web_conv_${generateUUID()}`,
        profileId: profileId,
        interfaceType: 'web',
      });
    },
    [conversationId, sendStreamingMessage, profileId]
  );

  const convertMessage = useCallback((message: Message) => {
    return message;
  }, []);

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || isStreaming,
    onNew: handleNew,
    convertMessage,
  });

  return (
    <>
      <NavHeader />
      <div className="flex min-h-screen flex-col bg-background">
        {/* Header */}
        <div className="sticky top-0 z-50 flex items-center gap-4 border-b bg-background/95 p-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            aria-label="Toggle sidebar"
            className="lg:hidden"
          >
            ☰
          </Button>
          <h1 className="text-xl font-semibold">Chat</h1>
        </div>

        {/* Body */}
        <div className="flex flex-1 overflow-hidden">
          {/* Mobile overlay */}
          {sidebarOpen && isMobile && (
            <div
              className="fixed inset-0 z-40 bg-black/50"
              onClick={() => setSidebarOpen(false)}
              aria-hidden="true"
            />
          )}

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
            />
          )}

          {/* Sidebar - Mobile Sheet */}
          {isMobile && (
            <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
              <SheetContent side="left" className="w-80 p-0">
                <ConversationSidebar
                  conversations={conversations}
                  conversationsLoading={conversationsLoading}
                  currentConversationId={conversationId}
                  onConversationSelect={handleConversationSelect}
                  onNewChat={handleNewChat}
                  isOpen={true}
                  onRefresh={fetchConversations}
                />
              </SheetContent>
            </Sheet>
          )}

          {/* Main content */}
          <div className="flex min-w-0 flex-1 flex-col">
            <main className="flex flex-1 flex-col overflow-hidden">
              <AssistantRuntimeProvider runtime={runtime}>
                <div className="flex flex-1 flex-col overflow-hidden">
                  <div className="border-b bg-muted/50 p-6">
                    <h2 className="text-xl font-semibold">Family Assistant Chat</h2>
                    {conversationId && (
                      <div className="mt-1 text-xs text-muted-foreground font-mono">
                        Conversation: {conversationId.substring(0, 20)}...
                      </div>
                    )}
                  </div>
                  <Thread />
                </div>
              </AssistantRuntimeProvider>
            </main>
            <footer className="border-t p-4 text-center text-sm text-muted-foreground bg-background">
              <p>&copy; {new Date().getFullYear()} Family Assistant</p>
            </footer>
          </div>
        </div>
      </div>
    </>
  );
};

export default ChatApp;
