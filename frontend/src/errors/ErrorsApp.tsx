import React, { useEffect } from 'react';
import { Routes, Route } from 'react-router-dom';
import ErrorsList from './components/ErrorsList';
import ErrorDetail from './components/ErrorDetail';
import './errors.css';

const ErrorsApp: React.FC = () => {
  // Set the page title and signal app is ready
  useEffect(() => {
    document.title = 'Error Logs - Family Assistant';

    // Signal that the app structure is ready
    // Child components will handle their own loading states
    document.documentElement.setAttribute('data-app-ready', 'true');

    return () => {
      // Clean up on unmount
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div className="errors-app">
      <main className="container">
        <Routes>
          <Route path="/" element={<ErrorsList />} />
          <Route path="/:errorId" element={<ErrorDetail />} />
        </Routes>
      </main>
    </div>
  );
};

export default ErrorsApp;
