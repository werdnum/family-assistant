import React, { useCallback, useEffect, useState } from 'react';
import { Route, Routes, useNavigate } from 'react-router-dom';
import ConversationsList from './components/ConversationsList';
import ConversationView from './components/ConversationView';

const HistoryApp = () => {
  const navigate = useNavigate();
  const [childLoaded, setChildLoaded] = useState(false);

  // Notify parent that child component has finished loading
  const handleChildLoaded = useCallback(() => {
    setChildLoaded(true);
  }, []);

  // Set document title and signal app readiness
  useEffect(() => {
    document.title = 'History - Family Assistant';

    // Set data-app-ready when child component is loaded
    if (childLoaded) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }

    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [childLoaded]);

  // Reset loaded state when route changes
  useEffect(() => {
    setChildLoaded(false);
  }, [navigate]);

  return (
    <div>
      <Routes>
        {/* Conversations list page - matches /history */}
        <Route index element={<ConversationsList onLoaded={handleChildLoaded} />} />

        {/* Individual conversation view - matches /history/:conversationId */}
        <Route
          path=":conversationId"
          element={
            <ConversationView
              onBackToList={() => navigate('/history')}
              onLoaded={handleChildLoaded}
            />
          }
        />
      </Routes>
    </div>
  );
};

export default HistoryApp;
