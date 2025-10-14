import React, { useState, useEffect, useCallback } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import ConversationsList from './components/ConversationsList';
import ConversationView from './components/ConversationView';

const HistoryApp: React.FC = () => {
  const navigate = useNavigate();
  const [childLoaded, setChildLoaded] = useState<boolean>(false);

  const handleChildLoaded = useCallback(() => {
    setChildLoaded(true);
  }, []);

  useEffect(() => {
    document.title = 'History - Family Assistant';

    if (childLoaded) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }

    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [childLoaded]);

  useEffect(() => {
    setChildLoaded(false);
  }, [navigate]);

  return (
    <div>
      <Routes>
        <Route path="/" element={<ConversationsList onLoaded={handleChildLoaded} />} />
        <Route
          path="/:conversationId"
          element={<ConversationView onBackToList={() => navigate('/history')} />}
        />
      </Routes>
    </div>
  );
};

export default HistoryApp;
