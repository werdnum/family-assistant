import React from 'react';
import ReactDOM from 'react-dom/client';
import AppRouter from './AppRouter';

// Import Tailwind CSS and custom styles
import '../styles/globals.css';
import '../chat/chat.css';
import '../chat/thread.css';
import '../tools/tools.css';
import '../errors/errors.css';

// Ensure the DOM is ready before mounting
function mountApp() {
  const container = document.getElementById('app-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <AppRouter />
      </React.StrictMode>
    );
    // Add a data attribute to indicate React has mounted (used by tests)
    container.setAttribute('data-react-mounted', 'true');
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
