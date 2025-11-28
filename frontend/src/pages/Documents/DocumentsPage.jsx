import React, { useEffect } from 'react';
import { Route, Routes } from 'react-router-dom';
import DocumentDetail from './DocumentDetail';
import DocumentsListWithDataTable from './DocumentsListWithDataTable';
import DocumentUpload from './DocumentUpload';

const DocumentsPage = () => {
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
      <Route index element={<DocumentsListWithDataTable />} />
      <Route path="upload" element={<DocumentUpload />} />
      <Route path=":id" element={<DocumentDetail />} />
    </Routes>
  );
};

export default DocumentsPage;
