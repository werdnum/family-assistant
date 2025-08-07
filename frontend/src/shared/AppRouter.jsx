import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Layout from './Layout';
import ChatPage from '../chat/ChatPage';
import ToolsApp from '../tools/ToolsApp';
import ErrorsApp from '../errors/ErrorsApp';
import ContextPage from './ContextPage';
import NotesApp from '../notes/NotesApp';
import TasksApp from '../tasks/TasksApp';
import EventListenersApp from '../pages/EventListeners/EventListenersApp';
import EventsApp from '../pages/Events/EventsApp';
import HistoryApp from '../pages/History/HistoryApp';
import DocumentationApp from '../pages/Documentation/DocumentationApp';
import TokenManagement from '../pages/Settings/TokenManagement';
import DocumentsPage from '../pages/Documents/DocumentsPage';

const FallbackRedirect = () => {
  useEffect(() => {
    window.location.href = window.location.pathname;
  }, []);

  return (
    <div>
      <p>Page not yet converted to React. Redirecting...</p>
    </div>
  );
};

const AppRouter = () => {
  return (
    <BrowserRouter>
      <Routes>
        {/* Chat routes */}
        <Route
          path="/chat"
          element={
            <Layout>
              <ChatPage />
            </Layout>
          }
        />

        {/* Tools routes */}
        <Route
          path="/tools"
          element={
            <Layout>
              <ToolsApp />
            </Layout>
          }
        />

        {/* Errors routes */}
        <Route
          path="/errors/*"
          element={
            <Layout>
              <ErrorsApp />
            </Layout>
          }
        />

        {/* Context page (test conversion) */}
        <Route
          path="/context"
          element={
            <Layout>
              <ContextPage />
            </Layout>
          }
        />

        {/* Notes routes */}
        <Route
          path="/notes/*"
          element={
            <Layout>
              <NotesApp />
            </Layout>
          }
        />

        {/* Tasks routes */}
        <Route
          path="/tasks/*"
          element={
            <Layout>
              <TasksApp />
            </Layout>
          }
        />

        {/* Event Listeners routes */}
        <Route
          path="/event-listeners/*"
          element={
            <Layout>
              <EventListenersApp />
            </Layout>
          }
        />

        {/* Events routes */}
        <Route
          path="/events/*"
          element={
            <Layout>
              <EventsApp />
            </Layout>
          }
        />

        {/* History routes */}
        <Route
          path="/history/*"
          element={
            <Layout>
              <HistoryApp />
            </Layout>
          }
        />

        {/* Documentation routes */}
        <Route
          path="/docs/*"
          element={
            <Layout>
              <DocumentationApp />
            </Layout>
          }
        />

        {/* Settings routes */}
        <Route
          path="/settings/tokens"
          element={
            <Layout>
              <TokenManagement />
            </Layout>
          }
        />

        {/* Documents routes */}
        <Route
          path="/documents/*"
          element={
            <Layout>
              <DocumentsPage />
            </Layout>
          }
        />

        {/* Default redirect to chat */}
        <Route path="/" element={<Navigate to="/chat" replace />} />

        {/* Catch-all for unmatched routes - redirect to external pages for now */}
        <Route path="*" element={<FallbackRedirect />} />
      </Routes>
    </BrowserRouter>
  );
};

export default AppRouter;
