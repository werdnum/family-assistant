import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import NavHeader from '../chat/NavHeader';
import ErrorsList from './components/ErrorsList';
import ErrorDetail from './components/ErrorDetail';

const ErrorsApp = () => {
  return (
    <Router>
      <div className="errors-app">
        <NavHeader currentPage="errors" />
        <main className="container">
          <Routes>
            <Route path="/errors" element={<ErrorsList />} />
            <Route path="/errors/:errorId" element={<ErrorDetail />} />
            {/* Redirect root to errors list */}
            <Route path="/" element={<Navigate to="/errors" replace />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
};

export default ErrorsApp;
