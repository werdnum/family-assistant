import React from 'react';
import ReactDOM from 'react-dom/client';
import AppRouter from './AppRouter';
import { isEmbeddedPage } from './embeddedNavigation';
import SessionBridgeGate from './SessionBridgeGate';
import { scheduleServiceWorkerUpdates } from './serviceWorkerUpdates';
import { ThemeProvider } from './ThemeProvider';
import { initializeErrorHandlers } from '../errors/errorHandlers';

// Import Tailwind CSS and custom styles
import '../styles/globals.css';

// Initialize global error handlers to capture uncaught errors
initializeErrorHandlers();

// Ensure the DOM is ready before mounting
function mountApp() {
  const container = document.getElementById('app-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ThemeProvider defaultTheme="system" storageKey="family-assistant-theme">
          {isEmbeddedPage() ? (
            <AppRouter />
          ) : (
            <SessionBridgeGate>
              <AppRouter />
            </SessionBridgeGate>
          )}
        </ThemeProvider>
      </React.StrictMode>
    );
    // Note: Individual apps (ChatApp, ToolsApp, etc.) set data-app-ready when they're fully initialized
  } else {
    console.error('Could not find app-root element');
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountApp);
} else {
  mountApp();
}

// Register service worker for PWA functionality
if (typeof window !== 'undefined' && !isEmbeddedPage() && 'serviceWorker' in window.navigator) {
  window.navigator.serviceWorker
    .register('/sw.js', { scope: '/' })
    .then((registration) => {
      scheduleServiceWorkerUpdates(registration);
    })
    .catch((error) => {
      console.error('Service Worker registration failed:', error);
    });
}
