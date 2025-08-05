import React from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import EventListenersList from './components/EventListenersList';
import EventListenerDetail from './components/EventListenerDetail';
import EventListenerForm from './components/EventListenerForm';

const EventListenersApp = () => {
  const navigate = useNavigate();

  return (
    <div>
      <Routes>
        {/* Event listeners list page - matches /event-listeners */}
        <Route index element={<EventListenersList />} />

        {/* New event listener page - matches /event-listeners/new */}
        <Route
          path="new"
          element={
            <EventListenerForm
              isEdit={false}
              onSuccess={(id) => navigate(`/event-listeners/${id}`)}
              onCancel={() => navigate('/event-listeners')}
            />
          }
        />

        {/* Event listener detail page - matches /event-listeners/:id */}
        <Route path=":id" element={<EventListenerDetail />} />

        {/* Edit event listener page - matches /event-listeners/:id/edit */}
        <Route
          path=":id/edit"
          element={
            <EventListenerForm
              isEdit={true}
              onSuccess={(id) => navigate(`/event-listeners/${id}`)}
              onCancel={(id) => navigate(`/event-listeners/${id}`)}
            />
          }
        />
      </Routes>
    </div>
  );
};

export default EventListenersApp;
