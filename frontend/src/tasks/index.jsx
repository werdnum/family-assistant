import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import Layout from '../shared/Layout';
import TasksApp from './TasksApp';

// Import styles
import 'simpledotcss/simple.css';
import '../custom.css';

// Mount the tasks app
function mountTasksApp() {
  const container = document.getElementById('tasks-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <BrowserRouter>
          <Layout>
            <TasksApp />
          </Layout>
        </BrowserRouter>
      </React.StrictMode>
    );
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountTasksApp);
} else {
  mountTasksApp();
}
