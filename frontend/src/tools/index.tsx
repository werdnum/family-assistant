import React from 'react';
import ReactDOM from 'react-dom/client';
import ToolsApp from './ToolsApp';

// Import Simple.css and custom styles for consistency
import 'simpledotcss/simple.css';
import '../custom.css';
import './tools.css';

// Mount the tools app
const container = document.getElementById('tools-root');
if (container) {
  const root = ReactDOM.createRoot(container);
  root.render(
    <React.StrictMode>
      <ToolsApp />
    </React.StrictMode>
  );
} else {
  console.error('Could not find tools-root element');
}
