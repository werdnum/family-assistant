import React from 'react';
import { Routes, Route } from 'react-router-dom';
import DocumentsList from './DocumentsList';
import DocumentUpload from './DocumentUpload';
import DocumentDetail from './DocumentDetail';

const DocumentsPage = () => {
  return (
    <Routes>
      <Route index element={<DocumentsList />} />
      <Route path="upload" element={<DocumentUpload />} />
      <Route path=":id" element={<DocumentDetail />} />
    </Routes>
  );
};

export default DocumentsPage;
