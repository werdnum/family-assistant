import React, { useState, useCallback, useEffect, useRef } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Thread, ThreadLoading } from './Thread';
import NavHeader from './NavHeader';
import ConversationSidebar from './ConversationSidebar';
import { useStreamingResponse } from './useStreamingResponse';
import './chat.css';
import './thread.css';

const ChatApp = ({ profileId = 'default_assistant' } = {}) => {
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const streamingMessageIdRef = useRef(null);

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
  const handleStreamingMessage = useCallback((content) => {
    // Update the assistant message with streaming content
    if (streamingMessageIdRef.current) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === streamingMessageIdRef.current
            ? { ...msg, content: [{ type: 'text', text: content }] }
            : msg
        )
      );
    }
  }, []);

  const handleStreamingError = useCallback((error, _metadata) => {
    console.error('Streaming error:', error, _metadata);
    // Update the existing placeholder message with error content
    if (streamingMessageIdRef.current) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === streamingMessageIdRef.current
            ? {
                ...msg,
                content: [
                  { type: 'text', text: 'Sorry, I encountered an error processing your message.' },
                ],
              }
            : msg
        )
      );
      // Reset the ref
      streamingMessageIdRef.current = null;
    }
  }, []);

  const handleStreamingComplete = useCallback(
    ({ content, toolCalls }) => {
      // Update the message with any tool calls if present
      if (toolCalls && toolCalls.length > 0 && streamingMessageIdRef.current) {
        const contentParts = [];
        if (content) {
          contentParts.push({ type: 'text', text: content });
        }
        toolCalls.forEach((tc) => {
          contentParts.push({
            type: 'tool-call',
            toolCallId: tc.id,
            toolName: tc.name,
            args: tc.arguments,
            result: 'Tool execution in progress...',
          });
        });
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === streamingMessageIdRef.current ? { ...msg, content: contentParts } : msg
          )
        );
      }
      // Refresh conversations after message is sent
      fetchConversations();
      // Clear refs
      streamingMessageIdRef.current = null;
    },
    [fetchConversations]
  );

  // Initialize streaming hook
  const { sendStreamingMessage, isStreaming } = useStreamingResponse({
    onMessage: handleStreamingMessage,
    onError: handleStreamingError,
    onComplete: handleStreamingComplete,
  });

  // Handle window resize
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Initialize conversation ID from URL or localStorage
  useEffect(() => {
    // Fetch conversations list first
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
      // Update URL without triggering reload
      window.history.replaceState({}, '', `/chat?conversation_id=${lastConversationId}`);
    } else {
      // Create new conversation
      handleNewChat();
    }
  }, []);

  // Load messages for a conversation
  const loadConversationMessages = async (convId) => {
    try {
      setIsLoading(true);
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages`);
      if (response.ok) {
        const data = await response.json();

        // Process messages and merge tool responses with their assistant messages
        const processedMessages = [];
        const toolResponses = new Map(); // Map tool_call_id to tool response

        // First pass: collect tool responses
        data.messages.forEach((msg) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            toolResponses.set(msg.tool_call_id, msg.content || 'Tool executed successfully');
          }
        });

        // Second pass: process messages, merging tool calls with their responses
        data.messages.forEach((msg) => {
          // Skip tool messages as they're merged with assistant messages
          if (msg.role === 'tool') {
            return;
          }

          // Handle assistant messages that might have tool calls
          if (msg.role === 'assistant' && msg.tool_calls_info) {
            let toolCallsInfo;
            try {
              toolCallsInfo =
                typeof msg.tool_calls_info === 'string'
                  ? JSON.parse(msg.tool_calls_info)
                  : msg.tool_calls_info;
            } catch (_e) {
              toolCallsInfo = null;
            }

            if (toolCallsInfo && toolCallsInfo.tool_calls) {
              // Convert tool calls to content parts with their results
              const content = [];
              if (msg.content) {
                content.push({ type: 'text', text: msg.content });
              }

              toolCallsInfo.tool_calls.forEach((toolCall) => {
                const toolResponse = toolResponses.get(toolCall.id);
                content.push({
                  type: 'tool-call',
                  toolCallId: toolCall.id,
                  toolName: toolCall.name,
                  args: toolCall.arguments,
                  result: toolResponse ?? 'Tool result not available', // Provide fallback for missing results
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
          }

          // Regular messages (user, assistant without tool calls, system)
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
  const handleConversationSelect = (convId) => {
    setConversationId(convId);
    localStorage.setItem('lastConversationId', convId);
    window.history.pushState({}, '', `/chat?conversation_id=${convId}`);
    loadConversationMessages(convId);

    // Close sidebar on mobile after selection
    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };

  // Handle new chat creation
  const handleNewChat = () => {
    const newConvId = `web_conv_${crypto.randomUUID()}`;
    setConversationId(newConvId);
    setMessages([]);
    localStorage.setItem('lastConversationId', newConvId);
    window.history.pushState({}, '', `/chat?conversation_id=${newConvId}`);

    // Note: The conversation list will be refreshed after the first message is sent
    // since a conversation only exists in the backend after it has messages

    // Close sidebar on mobile after creating new chat
    if (window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };

  // Handle new messages from the user
  const handleNew = useCallback(
    async (message) => {
      // Add user message to state
      const userMessage = {
        id: `msg_${Date.now()}`,
        role: 'user',
        content: [{ type: 'text', text: message.content[0].text }],
        createdAt: new Date(),
      };

      setMessages((prev) => [...prev, userMessage]);

      // Create placeholder assistant message for streaming
      const assistantMessageId = `msg_${Date.now()}_assistant`;
      const assistantMessage = {
        id: assistantMessageId,
        role: 'assistant',
        content: [{ type: 'text', text: '' }], // Start with empty content
        createdAt: new Date(),
      };
      setMessages((prev) => [...prev, assistantMessage]);

      // Store the message ID for streaming updates
      streamingMessageIdRef.current = assistantMessageId;

      // Send message using streaming API
      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: conversationId || `web_conv_${crypto.randomUUID()}`,
        profileId: profileId,
        interfaceType: 'web',
      });
    },
    [conversationId, sendStreamingMessage, profileId]
  );

  // Convert backend message format to assistant-ui format
  const convertMessage = useCallback((message) => {
    // Messages are already in the correct format from our processing
    return message;
  }, []);

  // Create the runtime
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || isStreaming || !conversationId, // Prevent sending messages until conversationId is ready or while streaming
    onNew: handleNew,
    convertMessage,
  });

  return (
    <div className={`chat-app-wrapper ${sidebarOpen ? 'with-sidebar' : ''}`}>
      <NavHeader />
      <div className="chat-app-header">
        <button
          className="sidebar-toggle"
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label="Toggle sidebar"
        >
          ☰
        </button>
        <h1>Chat</h1>
      </div>
      <div className="chat-app-body">
        {/* Overlay when sidebar is open on mobile */}
        {sidebarOpen && isMobile && (
          <div
            className="sidebar-overlay"
            onClick={() => setSidebarOpen(false)}
            aria-hidden="true"
          />
        )}
        <ConversationSidebar
          conversations={conversations}
          conversationsLoading={conversationsLoading}
          currentConversationId={conversationId}
          onConversationSelect={handleConversationSelect}
          onNewChat={handleNewChat}
          isOpen={sidebarOpen}
          onRefresh={fetchConversations}
        />
        <div className="chat-main-content">
          <main>
            <AssistantRuntimeProvider runtime={runtime}>
              <div className="chat-container">
                <div className="chat-info">
                  <h2>Family Assistant Chat</h2>
                  {conversationId && (
                    <div className="conversation-id">
                      Conversation: {conversationId.substring(0, 20)}...
                    </div>
                  )}
                </div>
                <Thread />
                {isLoading && messages.length > 0 && <ThreadLoading />}
              </div>
            </AssistantRuntimeProvider>
          </main>
          <footer>
            <p>&copy; {new Date().getFullYear()} Family Assistant</p>
          </footer>
        </div>
      </div>
    </div>
  );
};

export default ChatApp;
