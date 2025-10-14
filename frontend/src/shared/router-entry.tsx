import React from 'react';
import ReactDOM from 'react-dom/client';
import AppRouter from './AppRouter';
import { ThemeProvider } from './ThemeProvider';

// Import Tailwind CSS and custom styles
import '../styles/globals.css';

// Ensure the DOM is ready before mounting
function mountApp() {
  const container = document.getElementById('app-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ThemeProvider defaultTheme="system" storageKey="family-assistant-theme">
          <AppRouter />
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
