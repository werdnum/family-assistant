import React from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import DocumentationList from './components/DocumentationList';
import DocumentationView from './components/DocumentationView';

const DocumentationApp = () => {
  const navigate = useNavigate();

  return (
    <div>
      <Routes>
        {/* Documentation list page - matches /docs */}
        <Route index element={<DocumentationList />} />

        {/* Individual documentation view - matches /docs/:filename */}
        <Route
          path=":filename"
          element={<DocumentationView onBackToList={() => navigate('/docs')} />}
        />
      </Routes>
    </div>
  );
};

export default DocumentationApp;
