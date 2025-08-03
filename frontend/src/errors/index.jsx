import React from 'react';
import ReactDOM from 'react-dom/client';
import ErrorsApp from './ErrorsApp';

// Import Simple.css and custom styles for consistency
import 'simpledotcss/simple.css';
import '../custom.css';
import './errors.css';

// Ensure the DOM is ready before mounting
function mountErrorsApp() {
  const container = document.getElementById('errors-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <ErrorsApp />
      </React.StrictMode>
    );
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountErrorsApp);
} else {
  mountErrorsApp();
}
