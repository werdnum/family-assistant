import React from 'react';
import ReactDOM from 'react-dom/client';
import ChatApp from './ChatApp';
import { ThemeProvider } from '../shared/ThemeProvider';

// Import Tailwind CSS and custom styles
import '../styles/globals.css';
import '../custom.css';

// Ensure the DOM is ready before mounting
function mountChatApp() {
  const container = document.getElementById('chat-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ThemeProvider defaultTheme="system" storageKey="family-assistant-theme">
          <ChatApp />
        </ThemeProvider>
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
