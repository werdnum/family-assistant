import React from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import EventsList from './components/EventsList';
import EventDetail from './components/EventDetail';

const EventsApp = () => {
  const navigate = useNavigate();

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
