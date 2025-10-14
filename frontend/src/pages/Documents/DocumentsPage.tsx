import React, { useEffect } from 'react';
import { Routes, Route } from 'react-router-dom';
import DocumentsListWithDataTable from './DocumentsListWithDataTable';
import DocumentUpload from './DocumentUpload';
import DocumentDetail from './DocumentDetail';

const DocumentsPage: React.FC = () => {
  // Set page title and signal app is ready
  useEffect(() => {
    document.title = 'Documents - Family Assistant';

    // Signal that app is ready (router is mounted, child components handle their own loading)
    document.getElementById('app-root')?.setAttribute('data-app-ready', 'true');

    return () => {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <Routes>
      <Route path="/" element={<DocumentsListWithDataTable />} />
      <Route path="/upload" element={<DocumentUpload />} />
      <Route path="/:id" element={<DocumentDetail />} />
    </Routes>
  );
};

export default DocumentsPage;
