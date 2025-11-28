import React, { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

// Lazy load all route components for code splitting
const Layout = lazy(() => import('./Layout.tsx'));
const ChatPage = lazy(() => import('../chat/ChatApp'));
const ToolsApp = lazy(() => import('../tools/ToolsApp'));
const ErrorsApp = lazy(() => import('../errors/ErrorsApp'));
const ContextPage = lazy(() => import('./ContextPage.tsx'));
const NotesApp = lazy(() => import('../notes/NotesApp'));
const TasksApp = lazy(() => import('../tasks/TasksApp'));

const AutomationsApp = lazy(() => import('../pages/Automations/AutomationsApp'));
const EventsApp = lazy(() => import('../pages/Events/EventsApp'));
const HistoryApp = lazy(() => import('../pages/History/HistoryApp'));
const DocumentationApp = lazy(() => import('../pages/Documentation/DocumentationApp'));
const TokenManagement = lazy(() => import('../pages/Settings/TokenManagement'));
const DocumentsPage = lazy(() => import('../pages/Documents/DocumentsPage'));
const VectorSearchPage = lazy(() => import('../pages/VectorSearch/VectorSearchPage'));
const VoicePage = lazy(() => import('../voice/VoicePage'));

// Loading component for Suspense fallback
const LoadingSpinner = () => (
  <div
    data-loading-indicator="true"
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

const withLayout = (element) => (
  <Suspense fallback={<LoadingSpinner />}>
    <Layout>
      <Suspense fallback={<LoadingSpinner />}>{element}</Suspense>
    </Layout>
  </Suspense>
);

const AppRouter = () => {
  return (
    <BrowserRouter>
      <Routes>
        {/* Chat routes - no Layout wrapper as ChatApp has its own complete UI */}
        <Route
          path="/chat"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <ChatPage />
            </Suspense>
          }
        />

        {/* Tools routes */}
        <Route path="/tools" element={withLayout(<ToolsApp />)} />

        {/* Errors routes */}
        <Route path="/errors/*" element={withLayout(<ErrorsApp />)} />

        {/* Context page (test conversion) */}
        <Route path="/context" element={withLayout(<ContextPage />)} />

        {/* Notes routes */}
        <Route path="/notes/*" element={withLayout(<NotesApp />)} />

        {/* Tasks routes */}
        <Route path="/tasks/*" element={withLayout(<TasksApp />)} />

        {/* Event Listeners routes */}

        {/* Automations routes */}
        <Route path="/automations/*" element={withLayout(<AutomationsApp />)} />

        {/* Events routes */}
        <Route path="/events/*" element={withLayout(<EventsApp />)} />

        {/* History routes */}
        <Route path="/history/*" element={withLayout(<HistoryApp />)} />

        {/* Documentation routes */}
        <Route path="/docs/*" element={withLayout(<DocumentationApp />)} />

        {/* Settings routes */}
        <Route path="/settings/tokens" element={withLayout(<TokenManagement />)} />

        {/* Documents routes */}
        <Route path="/documents/*" element={withLayout(<DocumentsPage />)} />

        {/* Vector Search routes */}
        <Route path="/vector-search/*" element={withLayout(<VectorSearchPage />)} />

        {/* Voice mode - no Layout wrapper as VoicePage has its own complete UI */}
        <Route
          path="/voice"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <VoicePage />
            </Suspense>
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
