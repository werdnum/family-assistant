import React from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import ConversationsList from './components/ConversationsList';
import ConversationView from './components/ConversationView';

const HistoryApp = () => {
  const navigate = useNavigate();

  return (
    <div>
      <Routes>
        {/* Conversations list page - matches /history */}
        <Route index element={<ConversationsList />} />

        {/* Individual conversation view - matches /history/:conversationId */}
        <Route
          path=":conversationId"
          element={<ConversationView onBackToList={() => navigate('/history')} />}
        />
      </Routes>
    </div>
  );
};

export default HistoryApp;
