import React from 'react';
import { Routes, Route } from 'react-router-dom';
import TasksList from './components/TasksList';

const TasksApp = () => {
  return (
    <div>
      <Routes>
        {/* Tasks list page - matches /tasks */}
        <Route index element={<TasksList />} />
      </Routes>
    </div>
  );
};

export default TasksApp;
