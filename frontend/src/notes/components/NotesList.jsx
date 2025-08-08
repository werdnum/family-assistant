import React, { useState, useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';

const NotesList = () => {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const abortControllerRef = useRef(null);

  useEffect(() => {
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    fetchNotes(abortController.signal);

    return () => {
      abortController.abort();
    };
  }, []);

  const fetchNotes = async (signal) => {
    try {
      setLoading(true);
      setError(null);
      const response = await fetch('/api/notes/', { signal });
      if (!response.ok) {
        let errorText = `HTTP error! status: ${response.status}`;
        try {
          const errorData = await response.json();
          errorText = errorData.detail || errorText;
        } catch (_e) {
          // Ignore if response is not json
        }
        throw new Error(errorText);
      }
      const data = await response.json();
      setNotes(data);
    } catch (err) {
      // Don't log errors for aborted requests (happens during navigation)
      if (err.name !== 'AbortError' && !err.message?.includes('aborted')) {
        const message =
          err instanceof TypeError && err.message.includes('Failed to fetch')
            ? 'Could not connect to the API server. Please check your network connection and if the server is running.'
            : err.message;
        setError(message);
        // Only log real errors, not navigation-related aborts
        if (!err.message?.includes('Failed to fetch')) {
          console.error('Error fetching notes:', message);
        }
      }
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
      const abortController = new AbortController();
      abortControllerRef.current = abortController;
      await fetchNotes(abortController.signal);
    } catch (err) {
      setError(`Error deleting note: ${err.message}`);
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
          <Button>Add New Note</Button>
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
                    <Button size="sm">Edit</Button>
                  </Link>
                  <Button onClick={() => handleDelete(note.title)} variant="destructive" size="sm">
                    Delete
                  </Button>
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
