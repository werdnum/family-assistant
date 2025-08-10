import React, { useEffect } from 'react';
import { Routes, Route } from 'react-router-dom';
import ErrorsList from './components/ErrorsList';
import ErrorDetail from './components/ErrorDetail';
import './errors.css';

const ErrorsApp = () => {
  // Set the page title
  useEffect(() => {
    document.title = 'Error Logs - Family Assistant';
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
