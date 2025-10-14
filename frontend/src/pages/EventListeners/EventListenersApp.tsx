import React, { useEffect } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import EventListenersList from './components/EventListenersList';
import EventListenerDetail from './components/EventListenerDetail';
import EventListenerForm from './components/EventListenerForm';

const EventListenersApp: React.FC = () => {
  const navigate = useNavigate();

  // Signal that the app router is ready (for tests)
  // The router itself has no loading states - child components handle their own loading
  useEffect(() => {
    document.documentElement.setAttribute('data-app-ready', 'true');
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div>
      <Routes>
        {/* Event listeners list page - matches /event-listeners */}
        <Route path="/" element={<EventListenersList />} />

        {/* New event listener page - matches /event-listeners/new */}
        <Route
          path="/new"
          element={
            <EventListenerForm
              isEdit={false}
              onSuccess={(id: string) => navigate(`/event-listeners/${id}`)}
              onCancel={() => navigate('/event-listeners')}
            />
          }
        />

        {/* Event listener detail page - matches /event-listeners/:id */}
        <Route path="/:id" element={<EventListenerDetail />} />

        {/* Edit event listener page - matches /event-listeners/:id/edit */}
        <Route
          path="/:id/edit"
          element={
            <EventListenerForm
              isEdit={true}
              onSuccess={(id: string) => navigate(`/event-listeners/${id}`)}
              onCancel={(id: string | null) => navigate(`/event-listeners/${id}`)}
            />
          }
        />
      </Routes>
    </div>
  );
};

export default EventListenersApp;
