import React from 'react';
import ReactDOM from 'react-dom/client';
import './test-bench.css'; // Additional test bench specific styles
import { ToolTestBench } from './ToolTestBench';

// Mount the test bench app
const container = document.getElementById('test-bench-root');
if (container) {
  const root = ReactDOM.createRoot(container);
  root.render(
    <React.StrictMode>
      <ToolTestBench />
    </React.StrictMode>
  );
} else {
  console.error('Could not find test-bench-root element');
}
