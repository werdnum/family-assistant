import React from 'react';
import { Routes, Route } from 'react-router-dom';
import DocumentsListWithDataTable from './DocumentsListWithDataTable';
import DocumentUpload from './DocumentUpload';
import DocumentDetail from './DocumentDetail';

const DocumentsPage = () => {
  return (
    <Routes>
      <Route index element={<DocumentsListWithDataTable />} />
      <Route path="upload" element={<DocumentUpload />} />
      <Route path=":id" element={<DocumentDetail />} />
    </Routes>
  );
};

export default DocumentsPage;
