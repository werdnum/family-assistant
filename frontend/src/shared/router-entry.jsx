import React from 'react';
import ReactDOM from 'react-dom/client';
import AppRouter from './AppRouter';

// Import Simple.css and custom styles for consistency
import 'simpledotcss/simple.css';
import '../custom.css';
import '../chat/chat.css';
import '../chat/thread.css';
import '../tools/tools.css';
import '../errors/errors.css';

// Ensure the DOM is ready before mounting
function mountApp() {
  try {
    const container = document.getElementById('app-root');
    if (container) {
      const root = ReactDOM.createRoot(container);
      root.render(
        <React.StrictMode>
          <AppRouter />
        </React.StrictMode>
      );
      // Add a data attribute to indicate React has mounted
      container.setAttribute('data-react-mounted', 'true');
    } else {
      console.error('Could not find app-root element');
    }
  } catch (error) {
    console.error('Error mounting React app:', error);
    // Still set an attribute to help tests detect the error
    const container = document.getElementById('app-root');
    if (container) {
      container.setAttribute('data-react-error', error.message);
    }
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountApp);
} else {
  mountApp();
}
