import React, { useEffect, useState } from 'react';
import { Route, Routes } from 'react-router-dom';
import TasksList from './components/TasksList';

const TasksApp = () => {
  const [isLoading, setIsLoading] = useState(true);

  // Signal that app is ready when loading is complete
  useEffect(() => {
    if (!isLoading) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [isLoading]);

  return (
    <div>
      <Routes>
        {/* Tasks list page - matches /tasks */}
        <Route index element={<TasksList onLoadingChange={setIsLoading} />} />
      </Routes>
    </div>
  );
};

export default TasksApp;
