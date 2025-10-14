import React, { useEffect } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import DocumentationList from './components/DocumentationList';
import DocumentationView from './components/DocumentationView';

const DocumentationApp: React.FC = () => {
  const navigate = useNavigate();

  // Signal that app is ready (for tests)
  // DocumentationApp itself has no loading state - child routes handle their own loading
  useEffect(() => {
    document.documentElement.setAttribute('data-app-ready', 'true');
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div>
      <Routes>
        {/* Documentation list page - matches /docs */}
        <Route path="/" element={<DocumentationList />} />

        {/* Individual documentation view - matches /docs/:filename */}
        <Route
          path="/:filename"
          element={<DocumentationView onBackToList={() => navigate('/docs')} />}
        />
      </Routes>
    </div>
  );
};

export default DocumentationApp;
