import React, { useEffect } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import NotesListWithDataTable from './components/NotesListWithDataTable';
import NotesForm from './components/NotesForm';

const NotesApp: React.FC = () => {
  const navigate = useNavigate();

  useEffect(() => {
    document.title = 'Notes - Family Assistant';

    let timerId: number | null = null;

    const checkAndSetReady = () => {
      const hasLoadingIndicator = document.body.textContent?.includes('Loading note');

      if (!hasLoadingIndicator) {
        document.documentElement.setAttribute('data-app-ready', 'true');
      } else {
        timerId = window.setTimeout(checkAndSetReady, 100);
      }
    };

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
        <Route index element={<NotesListWithDataTable />} />
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
