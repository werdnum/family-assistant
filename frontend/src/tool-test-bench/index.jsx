import React from 'react';
import ReactDOM from 'react-dom/client';
import '../chat/chat.css'; // Reuse the chat styles
import '../chat/thread.css'; // Reuse the thread styles for tool UI
import './test-bench.css'; // Additional test bench specific styles
import { ToolTestBench } from './ToolTestBench';

// Mount the test bench app
const root = ReactDOM.createRoot(document.getElementById('test-bench-root'));
root.render(<ToolTestBench />);