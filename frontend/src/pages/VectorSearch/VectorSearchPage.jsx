import React, { useEffect } from 'react';
import { Route, Routes } from 'react-router-dom';
import VectorSearch from './VectorSearch';

const VectorSearchPage = () => {
  // Set page title and signal app is ready
  useEffect(() => {
    document.title = 'Vector Search - Family Assistant';

    // Signal that app is ready (router is mounted, child components handle their own loading)
    document.getElementById('app-root')?.setAttribute('data-app-ready', 'true');

    return () => {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <Routes>
      <Route path="/" element={<VectorSearch />} />
    </Routes>
  );
};

export default VectorSearchPage;
