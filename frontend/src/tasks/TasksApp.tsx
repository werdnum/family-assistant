import React, { useState, useEffect } from 'react';
import { Routes, Route } from 'react-router-dom';
import TasksList from './components/TasksList';

const TasksApp: React.FC = () => {
  const [isLoading, setIsLoading] = useState<boolean>(true);

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
        <Route path="/" element={<TasksList onLoadingChange={setIsLoading} />} />
      </Routes>
    </div>
  );
};

export default TasksApp;
