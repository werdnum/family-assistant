import React from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import NotesList from './components/NotesList';
import NotesForm from './components/NotesForm';

const NotesApp = () => {
  const navigate = useNavigate();

  return (
    <div>
      <Routes>
        {/* Notes list page - matches /notes */}
        <Route index element={<NotesList />} />

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
