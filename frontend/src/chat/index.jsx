import React from 'react';
import ReactDOM from 'react-dom/client';
import ChatApp from './ChatApp';
import './chat.css';

// Ensure the DOM is ready before mounting
function mountChatApp() {
  const container = document.getElementById('chat-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ChatApp />
      </React.StrictMode>
    );
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountChatApp);
} else {
  mountChatApp();
}