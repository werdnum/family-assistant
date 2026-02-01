import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Menu } from 'lucide-react';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { TooltipProvider } from '@/components/ui/tooltip';
import NavigationSheet from '../shared/NavigationSheet';
import { parseToolArguments } from '../utils/toolUtils';
import { generateUUID } from '../utils/uuid';
import { defaultAttachmentAdapter } from './attachmentAdapter';
import ConversationSidebar from './ConversationSidebar';
import { LOADING_MARKER } from './constants';
import { NotificationSettings } from './NotificationSettings';
import ProfileSelector from './ProfileSelector';
import { PushNotificationButton } from './PushNotificationButton';
import { Thread } from './Thread';
import { ToolConfirmationProvider } from './ToolConfirmationContext';
import {
  BackendAttachment,
  BackendConversationMessage,
  ChatAppProps,
  Conversation,
  ConversationMessagesResponse,
  Message,
  MessageContent,
} from './types';
import { useLiveMessageUpdates } from './useLiveMessageUpdates';
import { useNotifications } from './useNotifications';
import { useStreamingResponse } from './useStreamingResponse';

const ChatApp: React.FC<ChatAppProps> = ({ profileId = 'default_assistant' }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState<boolean>(true);
  const [profilesLoading, setProfilesLoading] = useState<boolean>(true);
  const [isMobile, setIsMobile] = useState<boolean>(window.innerWidth <= 768);
  const [currentProfileId, setCurrentProfileId] = useState<string>(() => {
    // Load saved profile from localStorage, fallback to prop
    return localStorage.getItem('selectedProfileId') || profileId;
  });
  const [notificationsEnabled, setNotificationsEnabled] = useState<boolean>(() => {
    // Load notification preference from localStorage
    const saved = localStorage.getItem('notificationsEnabled');
    return saved === 'true';
  });
  const streamingMessageIdRef = useRef<string | null>(null);
  const toolCallMessageIdRef = useRef<string | null>(null);
  const initialPromptProcessedRef = useRef(false);
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
    async (_toolCallId: string, requestId: string, approved: boolean) => {
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
    if (!content.trim()) {
      return;
    }

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
                status: { type: 'complete' as const },
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

    // Check for attach_to_response specifically (removed debug logging)

    if (toolCalls && toolCalls.length > 0 && targetMessageId) {
      setMessages((prev) => {
        const updatedMessages = prev.map((msg) => {
          if (msg.id === targetMessageId) {
            // This is the message to update.
            // It might be a 'loading' message, or it might already have text.
            toolCallMessageIdRef.current = msg.id;

            const existingTextContent =
              msg.content?.filter((part) => part.type === 'text' && part.text !== LOADING_MARKER) ||
              [];

            // Create new tool parts with new object references
            const toolParts: MessageContent[] = toolCalls.map((tc) => {
              const args = parseToolArguments(tc.arguments);
              const result: MessageContent = {
                type: 'tool-call' as const,
                toolCallId: tc.id as string,
                toolName: tc.name as string,
                args: args as Record<string, unknown> | undefined,
                argsText:
                  typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments),
              };
              // Add result if present
              if (tc.result !== undefined) {
                result.result = tc.result as string;
              }
              // Add attachments if present
              if (tc.attachments !== undefined && Array.isArray(tc.attachments)) {
                result.attachments = [...tc.attachments];
              }
              return result;
            });

            return {
              ...msg,
              content: [...existingTextContent, ...toolParts],
              isLoading: false,
              status: { type: 'running' as const },
            };
          }
          return msg;
        });

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
  }) as {
    sendStreamingMessage: (params: {
      prompt: string;
      conversationId: string;
      profileId: string;
      interfaceType: string;
      attachments?: Array<{ id: string; type: string; name: string; content: string }>;
    }) => Promise<void>;
    cancelStream: () => void;
    isStreaming: boolean;
  };

  // Load messages for a conversation
  const loadConversationMessages = useCallback(async (convId: string, background = false) => {
    try {
      // Cancel previous messages request if it exists
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }

      // Create new abort controller for messages
      const messagesAbortController = new AbortController();
      messagesAbortControllerRef.current = messagesAbortController;

      if (!background) {
        setIsLoading(true);
      }
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages`, {
        signal: messagesAbortController.signal,
      });
      if (response.ok) {
        const data = (await response.json()) as ConversationMessagesResponse;
        const processedMessages: Message[] = [];
        const toolResponses = new Map<string, string>();
        const toolAttachments = new Map<string, BackendAttachment[]>();

        // First pass: collect tool responses and attachments
        data.messages.forEach((msg: BackendConversationMessage) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            const responseContent =
              typeof msg.content === 'string'
                ? msg.content
                : Array.isArray(msg.content)
                  ? JSON.stringify(msg.content)
                  : 'Tool executed successfully';
            toolResponses.set(msg.tool_call_id, responseContent);

            // Collect attachments from tool messages for synthesis
            const toolMessageAttachments = msg.attachments;
            if (Array.isArray(toolMessageAttachments) && toolMessageAttachments.length > 0) {
              toolAttachments.set(msg.tool_call_id, toolMessageAttachments);
            }
          }
        });

        data.messages.forEach((msg: BackendConversationMessage) => {
          if (msg.role === 'tool') {
            return;
          }

          if (
            msg.role === 'assistant' &&
            ((msg.tool_calls && msg.tool_calls.length > 0) ||
              (msg.metadata?.attachments && msg.metadata.attachments.length > 0))
          ) {
            const content: MessageContent[] = [];
            if (msg.content) {
              // Handle content - filter out image_url if present
              if (typeof msg.content === 'string') {
                content.push({ type: 'text', text: msg.content });
              } else if (Array.isArray(msg.content)) {
                for (const part of msg.content) {
                  if (part.type === 'text') {
                    content.push({ type: 'text', text: part.text as string });
                  }
                  // Skip image_url content types
                }
              }
            }

            // Process explicit tool calls if present

            if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
              msg.tool_calls.forEach((toolCall) => {
                const toolResponse = toolResponses.get(toolCall.id);
                const toolName = toolCall.function?.name || toolCall.name || 'unknown';
                const argumentSource = toolCall.function?.arguments ?? toolCall.arguments;
                const args = parseToolArguments(argumentSource);
                const argsText =
                  typeof argumentSource === 'string' ? argumentSource : JSON.stringify(args);

                content.push({
                  type: 'tool-call',
                  toolCallId: toolCall.id,
                  toolName: toolName,
                  args: args as Record<string, unknown>,
                  argsText: argsText,
                  result: toolResponse ?? undefined,
                });
              });
            }

            // Synthesize attach_to_response for tool call attachments
            // This extracts attachments from tool messages and associates them with the
            // assistant message that made the tool call
            // Collect all attachments from tool calls in this message
            const allToolAttachments: BackendAttachment[] = [];
            const allAttachmentIds: string[] = [];

            if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
              msg.tool_calls.forEach((toolCall) => {
                const attachments = toolAttachments.get(toolCall.id);
                if (attachments && attachments.length > 0) {
                  allToolAttachments.push(...attachments);
                  attachments.forEach((att) => {
                    if (typeof att.attachment_id === 'string') {
                      allAttachmentIds.push(att.attachment_id);
                    }
                  });
                }
              });
            }

            // Also check for attachments in the assistant message's metadata
            // These are attachments queued by tools like attach_to_response
            const metadataAttachments = msg.metadata?.attachments;
            if (Array.isArray(metadataAttachments) && metadataAttachments.length > 0) {
              // Add attachments from metadata to the collection
              allToolAttachments.push(...metadataAttachments);
              metadataAttachments.forEach((att) => {
                if (typeof att.attachment_id === 'string') {
                  allAttachmentIds.push(att.attachment_id);
                }
              });
            }

            if (allToolAttachments.length > 0) {
              content.push({
                type: 'tool-call',
                toolCallId: `history_attach_tool_${msg.internal_id}`,
                toolName: 'attach_to_response',
                args: { attachment_ids: allAttachmentIds },
                argsText: JSON.stringify({ attachment_ids: allAttachmentIds }),
                result: JSON.stringify({
                  status: 'attachments_queued',
                  count: allToolAttachments.length,
                  attachments: allToolAttachments,
                }),
                attachments: allToolAttachments,
                artifact: {
                  attachments: allToolAttachments,
                },
              });
            }

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
              for (const part of msg.content) {
                if (part.type === 'text') {
                  const textValue = (part as { text?: unknown }).text;
                  if (typeof textValue === 'string') {
                    messageContent.push({ type: 'text', text: textValue });
                  }
                } else if (part.type === 'image_url') {
                  const imageUrl = (part as { image_url?: { url?: unknown } }).image_url?.url;
                  if (typeof imageUrl === 'string') {
                    attachments.push({
                      id: `att_${msg.internal_id}_${attachments.length}`,
                      type: 'image',
                      name: `Image ${attachments.length + 1}`,
                      content: imageUrl,
                    });
                  }
                }
              }
            }
          }

          // Handle attachments from the dedicated attachments field (new format)
          if (Array.isArray(msg.attachments)) {
            for (const attachment of msg.attachments) {
              const attachmentType =
                attachment.type === 'image' || attachment.type === 'document'
                  ? attachment.type
                  : 'file';
              const attachmentName =
                typeof attachment.name === 'string'
                  ? attachment.name
                  : `Attachment ${attachments.length + 1}`;
              const contentUrl =
                typeof attachment.content_url === 'string' ? attachment.content_url : undefined;

              attachments.push({
                id: `att_${msg.internal_id}_${attachments.length}`,
                type: attachmentType,
                name: attachmentName,
                content: contentUrl,
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
      if (!background) {
        setIsLoading(false);
      }
    }
  }, []);

  // Handle conversation selection (defined early for use in notification callback)
  const handleConversationSelect = useCallback(
    (convId: string) => {
      // Cancel any active streaming before switching conversations
      cancelStream();

      setConversationId(convId);
      localStorage.setItem('lastConversationId', convId);
      window.history.pushState({}, '', `/chat?conversation_id=${convId}`);
      loadConversationMessages(convId);

      if (window.innerWidth <= 768) {
        setSidebarOpen(false);
      }
    },
    [cancelStream, loadConversationMessages]
  );

  // Handle notification clicks - navigate to the conversation
  const handleNotificationClick = useCallback(
    (notifConversationId: string) => {
      if (notifConversationId !== conversationId) {
        handleConversationSelect(notifConversationId);
      }
    },
    [conversationId, handleConversationSelect]
  );

  // Initialize notifications (before handleLiveMessageUpdate which uses showNotification)
  const {
    isSupported: notificationsSupported,
    permission: notificationPermission,
    requestPermission: requestNotificationPermission,
    showNotification,
  } = useNotifications({
    enabled: notificationsEnabled,
    conversationId,
    onNotificationClick: handleNotificationClick,
  });

  // Handle notification preference changes
  const handleNotificationEnabledChange = useCallback((enabled: boolean) => {
    setNotificationsEnabled(enabled);
    localStorage.setItem('notificationsEnabled', String(enabled));
  }, []);

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
    const initialPrompt = urlParams.get('q');
    const lastConversationId = localStorage.getItem('lastConversationId');

    if (initialPrompt && !initialPromptProcessedRef.current) {
      // If there's a prompt, start a new chat with it
      const newConvId = `web_conv_${generateUUID()}`;
      setConversationId(newConvId);
      setMessages([]);
      localStorage.setItem('lastConversationId', newConvId);
    } else if (urlConversationId) {
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

  // Handle initial prompt from query parameter once runtime is ready
  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const initialPrompt = urlParams.get('q');

    if (
      initialPrompt &&
      runtime &&
      !initialPromptProcessedRef.current &&
      conversationId?.startsWith('web_conv_')
    ) {
      initialPromptProcessedRef.current = true;
      handleNew({ content: [{ text: initialPrompt }] });

      // Clean up URL
      const newUrl = new URL(window.location.href);
      newUrl.searchParams.delete('q');
      if (conversationId) {
        newUrl.searchParams.set('conversation_id', conversationId);
      }
      window.history.replaceState({}, '', newUrl.pathname + newUrl.search);
    }
  }, [runtime, conversationId, handleNew]);

  // Create a stable callback ref for SSE message updates
  const handleLiveMessageUpdate = useCallback(
    (update: {
      internal_id: string;
      timestamp: string;
      new_messages: boolean;
      role?: string;
      content?: string;
      conversation_id?: string;
    }) => {
      // Show notification if it's an assistant message
      if (
        update.role === 'assistant' &&
        update.content &&
        update.conversation_id &&
        update.internal_id
      ) {
        // Dedupe: Don't show notification if we're currently streaming
        // (this message is likely from the active streaming session)
        const isCurrentlyStreaming = isStreaming && update.conversation_id === conversationId;

        if (!isCurrentlyStreaming) {
          // Extract preview from content (first 100 chars)
          let preview = update.content;
          if (preview.length > 100) {
            preview = preview.substring(0, 97) + '...';
          }

          // Show notification
          showNotification({
            conversationId: update.conversation_id,
            messageId: update.internal_id,
            preview,
            timestamp: update.timestamp,
          });
        }
      }

      // Reload messages for the updated conversation
      // Skip reload if we're currently streaming to avoid race conditions
      // Use background loading to prevent disabling the input during updates
      if (update.conversation_id === conversationId && !isStreaming) {
        loadConversationMessages(conversationId, true);
      }
    },
    [conversationId, loadConversationMessages, showNotification, isStreaming]
  );

  // Set up live message updates via SSE
  useLiveMessageUpdates({
    conversationId,
    interfaceType: 'web',
    enabled: true,
    onMessageReceived: handleLiveMessageUpdate,
  });

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
        content: message.content.map((c) => ({ type: 'text' as const, text: c.text })),
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
      // @ts-expect-error - assistant-ui type mismatch with content item filtering
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
    // @ts-expect-error - assistant-ui type mismatch with onNew handler signature
    onNew: handleNew,
    // @ts-expect-error - assistant-ui type mismatch with convertMessage return type
    convertMessage,
    adapters: {
      // @ts-expect-error - assistant-ui type mismatch with attachment adapter
      attachments: defaultAttachmentAdapter,
    },
  });

  // Signal that app is ready (for tests)
  // Only set when runtime is ready AND initial data loading is complete
  useEffect(() => {
    if (runtime && !conversationsLoading && !profilesLoading) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [runtime, conversationsLoading, profilesLoading]);

  return (
    <TooltipProvider>
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
              onLoadingChange={setProfilesLoading}
            />
          </div>

          {/* Notification Settings */}
          <div className="flex items-center gap-2 ml-auto">
            <NotificationSettings
              enabled={notificationsEnabled}
              onEnabledChange={handleNotificationEnabledChange}
              permission={notificationPermission}
              onRequestPermission={requestNotificationPermission}
              isSupported={notificationsSupported}
            />

            <PushNotificationButton />

            {/* Main Navigation Menu */}
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
    </TooltipProvider>
  );
};

export default ChatApp;
