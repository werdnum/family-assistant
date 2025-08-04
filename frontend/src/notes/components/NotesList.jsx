import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';

const NotesList = () => {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchNotes();
  }, []);

  const fetchNotes = async () => {
    try {
      setLoading(true);
      const response = await fetch('/api/notes/');
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();
      setNotes(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (title) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Are you sure you want to delete the note "${title}"?`)) {
      return;
    }

    try {
      const response = await fetch(`/api/notes/${encodeURIComponent(title)}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error(`Failed to delete note: ${response.status}`);
      }

      // Refresh the notes list
      await fetchNotes();
    } catch (err) {
      // eslint-disable-next-line no-alert
      window.alert(`Error deleting note: ${err.message}`);
    }
  };

  if (loading) {
    return <div>Loading notes...</div>;
  }

  if (error) {
    return <div>Error loading notes: {error}</div>;
  }

  return (
    <div>
      <header>
        <h1>Notes</h1>
        <Link to="/notes/add">
          <button>Add New Note</button>
        </Link>
      </header>

      {notes.length === 0 ? (
        <p>No notes found.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Status</th>
              <th>Content</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {notes.map((note) => (
              <tr key={note.title}>
                <td>{note.title}</td>
                <td>
                  <span
                    className={`status-badge ${
                      note.include_in_prompt ? 'status-included' : 'status-excluded'
                    }`}
                  >
                    {note.include_in_prompt ? '✓ In Prompt' : '◯ Searchable'}
                  </span>
                </td>
                <td>
                  <pre>{note.content}</pre>
                </td>
                <td className="note-actions">
                  <Link to={`/notes/edit/${encodeURIComponent(note.title)}`}>
                    <button>Edit</button>
                  </Link>
                  <button onClick={() => handleDelete(note.title)} style={{ marginLeft: '0.5rem' }}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

export default NotesList;
