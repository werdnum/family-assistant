import React, { useEffect } from 'react';
import { Route, Routes, useNavigate } from 'react-router-dom';
import EventDetail from './components/EventDetail';
import EventsList from './components/EventsList';

const EventsApp = () => {
  const navigate = useNavigate();

  // Signal that app is ready for tests
  // EventsApp itself doesn't have loading states - child components handle their own loading
  // and display appropriate loading indicators
  useEffect(() => {
    document.documentElement.setAttribute('data-app-ready', 'true');

    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div>
      <Routes>
        {/* Events list page - matches /events */}
        <Route index element={<EventsList />} />

        {/* Individual event view - matches /events/:eventId */}
        <Route path=":eventId" element={<EventDetail onBackToList={() => navigate('/events')} />} />
      </Routes>
    </div>
  );
};

export default EventsApp;
