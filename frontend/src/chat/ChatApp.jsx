import React, { useState, useCallback } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Thread } from '@assistant-ui/react';
import NavHeader from './NavHeader';
import './chat.css';

const ChatApp = () => {
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  
  // Get conversation_id from URL params or create new one
  const urlParams = new URLSearchParams(window.location.search);
  const conversationId = urlParams.get('conversation_id') || `web_conv_${Date.now()}`;
  
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
          conversation_id: conversationId,
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
    isRunning: isLoading,
    onNew: handleNew,
    convertMessage,
  });
  
  return (
    <div className="chat-app-wrapper">
      <NavHeader />
      <main>
        <AssistantRuntimeProvider runtime={runtime}>
          <div className="chat-container">
            <div className="chat-info">
              <h2>Family Assistant Chat</h2>
              <div className="conversation-id">Conversation: {conversationId}</div>
            </div>
            <Thread />
          </div>
        </AssistantRuntimeProvider>
      </main>
      <footer>
        <p>&copy; {new Date().getFullYear()} Family Assistant</p>
      </footer>
    </div>
  );
};

export default ChatApp;