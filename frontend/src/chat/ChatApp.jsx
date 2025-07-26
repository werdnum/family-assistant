import React, { useState, useCallback, useEffect } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Thread } from '@assistant-ui/react';
import NavHeader from './NavHeader';
import ConversationSidebar from './ConversationSidebar';
import './chat.css';

const ChatApp = () => {
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  
  // Fetch conversations list
  const fetchConversations = async () => {
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
  };

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
        // Convert messages to the format expected by the UI
        const formattedMessages = data.messages.map(msg => ({
          id: `msg_${msg.internal_id}`,
          role: msg.role,
          content: msg.content ? [{ type: 'text', text: msg.content }] : [],
          createdAt: new Date(msg.timestamp)
        }));
        setMessages(formattedMessages);
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
    const newConvId = `web_conv_${Date.now()}`;
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
  const handleNew = useCallback(async (message) => {
    // Add user message to state
    const userMessage = {
      id: `msg_${Date.now()}`,
      role: 'user',
      content: [{ type: 'text', text: message.content[0].text }],
      createdAt: new Date()
    };
    
    setMessages(prev => [...prev, userMessage]);
    setIsLoading(true);
    
    try {
      // Send message to backend
      const response = await fetch('/api/v1/chat/send_message', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          prompt: message.content[0].text,
          conversation_id: conversationId || `web_conv_${Date.now()}`,
          profile_id: 'default_assistant' // You can make this configurable
        }),
      });
      
      if (!response.ok) {
        // Handle authentication errors
        if (response.status === 401) {
          window.location.href = '/login?next=/chat';
          return;
        }
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      
      // Add assistant response to state
      const assistantMessage = {
        id: `msg_${Date.now()}_assistant`,
        role: 'assistant',
        content: [{ type: 'text', text: data.reply }],
        createdAt: new Date()
      };
      
      setMessages(prev => [...prev, assistantMessage]);
      
      // Refresh conversations to update the sidebar with the new message
      fetchConversations();
    } catch (error) {
      console.error('Error sending message:', error);
      // Add error message
      const errorMessage = {
        id: `msg_${Date.now()}_error`,
        role: 'assistant',
        content: [{ type: 'text', text: 'Sorry, I encountered an error processing your message.' }],
        createdAt: new Date()
      };
      setMessages(prev => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  }, [conversationId]);
  
  // Convert backend message format to assistant-ui format
  const convertMessage = useCallback((message) => {
    return {
      id: message.id,
      role: message.role,
      content: message.content,
      createdAt: message.createdAt
    };
  }, []);
  
  // Create the runtime
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || !conversationId, // Prevent sending messages until conversationId is ready
    onNew: handleNew,
    convertMessage,
  });
  
  return (
    <div className={`chat-app-wrapper ${sidebarOpen ? 'with-sidebar' : ''}`}>
      <ConversationSidebar
        conversations={conversations}
        conversationsLoading={conversationsLoading}
        currentConversationId={conversationId}
        onConversationSelect={handleConversationSelect}
        onNewChat={handleNewChat}
        isOpen={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
        onRefresh={fetchConversations}
      />
      <div className="chat-main-content">
        <NavHeader />
        <main>
          <AssistantRuntimeProvider runtime={runtime}>
            <div className="chat-container">
              <div className="chat-info">
                <h2>Family Assistant Chat</h2>
                {conversationId && (
                  <div className="conversation-id">Conversation: {conversationId.substring(0, 20)}...</div>
                )}
              </div>
              <Thread />
            </div>
          </AssistantRuntimeProvider>
        </main>
        <footer>
          <p>&copy; {new Date().getFullYear()} Family Assistant</p>
        </footer>
      </div>
    </div>
  );
};

export default ChatApp;