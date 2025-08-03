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
  const container = document.getElementById('app-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <AppRouter />
      </React.StrictMode>
    );
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountApp);
} else {
  mountApp();
}
