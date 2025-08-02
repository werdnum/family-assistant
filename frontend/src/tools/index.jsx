import React from 'react';
import ReactDOM from 'react-dom/client';
import ToolsApp from './ToolsApp';

// Import Simple.css and custom styles for consistency
import 'simpledotcss/simple.css';
import '../custom.css';
import './tools.css';

// Mount the tools app
const root = ReactDOM.createRoot(document.getElementById('tools-root'));
root.render(<ToolsApp />);
