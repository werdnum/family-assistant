import React from 'react';
import ReactDOM from 'react-dom/client';
import '../styles/globals.css';
import './test-bench.css'; // Additional test bench specific styles
import SessionBridgeGate from '../shared/SessionBridgeGate';
import { ThemeProvider } from '../shared/ThemeProvider';
import { ToolTestBench } from './ToolTestBench';

// Mount the test bench app
const root = ReactDOM.createRoot(document.getElementById('test-bench-root'));
root.render(
  <React.StrictMode>
    <ThemeProvider defaultTheme="system" storageKey="family-assistant-theme">
      <SessionBridgeGate>
        <ToolTestBench />
      </SessionBridgeGate>
    </ThemeProvider>
  </React.StrictMode>
);
