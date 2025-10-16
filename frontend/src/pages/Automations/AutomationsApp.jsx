import React, { useEffect } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import AutomationsList from './components/AutomationsList';
import AutomationDetail from './components/AutomationDetail';
import CreateEventAutomation from './components/CreateEventAutomation';
import CreateScheduleAutomation from './components/CreateScheduleAutomation';

const AutomationsApp = () => {
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
        {/* Automations list page - matches /automations */}
        <Route index element={<AutomationsList />} />

        {/* New event automation page - matches /automations/create/event */}
        <Route
          path="create/event"
          element={
            <CreateEventAutomation
              onSuccess={(id) => navigate(`/automations/event/${id}`)}
              onCancel={() => navigate('/automations')}
            />
          }
        />

        {/* New schedule automation page - matches /automations/create/schedule */}
        <Route
          path="create/schedule"
          element={
            <CreateScheduleAutomation
              onSuccess={(id) => navigate(`/automations/schedule/${id}`)}
              onCancel={() => navigate('/automations')}
            />
          }
        />

        {/* Automation detail page - matches /automations/:type/:id */}
        <Route path=":type/:id" element={<AutomationDetail />} />
      </Routes>
    </div>
  );
};

export default AutomationsApp;
