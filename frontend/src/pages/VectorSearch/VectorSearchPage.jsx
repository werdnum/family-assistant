import React from 'react';
import { Routes, Route } from 'react-router-dom';
import VectorSearch from './VectorSearch';

const VectorSearchPage = () => {
  return (
    <Routes>
      <Route path="/" element={<VectorSearch />} />
    </Routes>
  );
};

export default VectorSearchPage;
