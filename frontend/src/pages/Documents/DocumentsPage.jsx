import React from 'react';
import { Routes, Route } from 'react-router-dom';
import DocumentsList from './DocumentsList';
import DocumentUpload from './DocumentUpload';

const DocumentsPage = () => {
  return (
    <Routes>
      <Route index element={<DocumentsList />} />
      <Route path="upload" element={<DocumentUpload />} />
    </Routes>
  );
};

export default DocumentsPage;
