import React from 'react';
import ReactDOM from 'react-dom/client';
import { ThemeProvider } from '../shared/ThemeProvider';
import ChatApp from './ChatApp';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { initializeErrorHandlers } from '../errors/errorHandlers';

// Import Tailwind CSS and custom styles
import '../styles/globals.css';
import '../custom.css';

// Initialize global error handlers to capture uncaught errors
initializeErrorHandlers();

// Ensure the DOM is ready before mounting
function mountChatApp() {
  const container = document.getElementById('chat-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ThemeProvider defaultTheme="system" storageKey="family-assistant-theme">
          <ErrorBoundary componentName="ChatApp">
            <ChatApp />
          </ErrorBoundary>
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
