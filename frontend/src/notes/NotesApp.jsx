import React, { useEffect } from 'react';
import { Route, Routes, useNavigate } from 'react-router-dom';
import NotesForm from './components/NotesForm';
import NotesListWithDataTable from './components/NotesListWithDataTable';

const NotesApp = () => {
  const navigate = useNavigate();

  // Set data-app-ready when the app is initialized
  // Child components (NotesListWithDataTable and NotesForm) handle their own loading states
  // by displaying loading indicators. Once those are gone, the app is effectively ready.
  useEffect(() => {
    // Set document title
    document.title = 'Notes - Family Assistant';

    let timerId = null;

    // Check if there's any loading text in the DOM, if not, mark as ready
    const checkAndSetReady = () => {
      // Look for common loading indicators in our child components
      const hasLoadingIndicator = document.body.textContent?.includes('Loading note');

      if (!hasLoadingIndicator) {
        document.documentElement.setAttribute('data-app-ready', 'true');
      } else {
        // If still loading, check again shortly
        timerId = window.setTimeout(checkAndSetReady, 100);
      }
    };

    // Initial check after a brief delay to allow child components to mount
    timerId = window.setTimeout(checkAndSetReady, 50);

    return () => {
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div>
      <Routes>
        {/* Notes list page - matches /notes */}
        <Route index element={<NotesListWithDataTable />} />

        {/* Add note page - matches /notes/add */}
        <Route
          path="add"
          element={
            <NotesForm
              isEdit={false}
              onSuccess={() => navigate('/notes')}
              onCancel={() => navigate('/notes')}
            />
          }
        />

        {/* Edit note page - matches /notes/edit/:title */}
        <Route
          path="edit/:title"
          element={
            <NotesForm
              isEdit={true}
              onSuccess={() => navigate('/notes')}
              onCancel={() => navigate('/notes')}
            />
          }
        />
      </Routes>
    </div>
  );
};

export default NotesApp;
