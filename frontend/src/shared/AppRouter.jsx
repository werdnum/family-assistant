import React, { useEffect, lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Layout from './Layout.tsx';

// Lazy load all route components for code splitting
const ChatPage = lazy(() => import('../chat/ChatPage'));
const ToolsApp = lazy(() => import('../tools/ToolsApp'));
const ErrorsApp = lazy(() => import('../errors/ErrorsApp'));
const ContextPage = lazy(() => import('./ContextPage'));
const NotesApp = lazy(() => import('../notes/NotesApp'));
const TasksApp = lazy(() => import('../tasks/TasksApp'));
const EventListenersApp = lazy(() => import('../pages/EventListeners/EventListenersApp'));
const EventsApp = lazy(() => import('../pages/Events/EventsApp'));
const HistoryApp = lazy(() => import('../pages/History/HistoryApp'));
const DocumentationApp = lazy(() => import('../pages/Documentation/DocumentationApp'));
const TokenManagement = lazy(() => import('../pages/Settings/TokenManagement'));
const DocumentsPage = lazy(() => import('../pages/Documents/DocumentsPage'));
const VectorSearchPage = lazy(() => import('../pages/VectorSearch/VectorSearchPage'));

// Loading component for Suspense fallback
const LoadingSpinner = () => (
  <div
    style={{
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      minHeight: '200px',
      fontSize: '1.2rem',
      color: '#666',
    }}
  >
    Loading...
  </div>
);

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
              <Suspense fallback={<LoadingSpinner />}>
                <ChatPage />
              </Suspense>
            </Layout>
          }
        />

        {/* Tools routes */}
        <Route
          path="/tools"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <ToolsApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Errors routes */}
        <Route
          path="/errors/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <ErrorsApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Context page (test conversion) */}
        <Route
          path="/context"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <ContextPage />
              </Suspense>
            </Layout>
          }
        />

        {/* Notes routes */}
        <Route
          path="/notes/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <NotesApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Tasks routes */}
        <Route
          path="/tasks/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <TasksApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Event Listeners routes */}
        <Route
          path="/event-listeners/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <EventListenersApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Events routes */}
        <Route
          path="/events/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <EventsApp />
              </Suspense>
            </Layout>
          }
        />

        {/* History routes */}
        <Route
          path="/history/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <HistoryApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Documentation routes */}
        <Route
          path="/docs/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <DocumentationApp />
              </Suspense>
            </Layout>
          }
        />

        {/* Settings routes */}
        <Route
          path="/settings/tokens"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <TokenManagement />
              </Suspense>
            </Layout>
          }
        />

        {/* Documents routes */}
        <Route
          path="/documents/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <DocumentsPage />
              </Suspense>
            </Layout>
          }
        />

        {/* Vector Search routes */}
        <Route
          path="/vector-search/*"
          element={
            <Layout>
              <Suspense fallback={<LoadingSpinner />}>
                <VectorSearchPage />
              </Suspense>
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
