import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';

const NotesForm = ({ isEdit, onSuccess, onCancel }) => {
  const { title: urlTitle } = useParams();
  const [formData, setFormData] = useState({
    title: '',
    content: '',
    include_in_prompt: true,
  });
  const [originalTitle, setOriginalTitle] = useState(null); // Track original title for edits
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [initialLoading, setInitialLoading] = useState(isEdit);

  useEffect(() => {
    if (isEdit && urlTitle) {
      fetchNote(urlTitle);
    }
  }, [isEdit, urlTitle]);

  const fetchNote = async (title) => {
    try {
      setInitialLoading(true);
      const response = await fetch(`/api/notes/${encodeURIComponent(title)}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch note: ${response.status}`);
      }
      const note = await response.json();
      setFormData({
        title: note.title,
        content: note.content,
        include_in_prompt: note.include_in_prompt,
      });
      setOriginalTitle(note.title); // Remember the original title
    } catch (err) {
      setError(err.message);
    } finally {
      setInitialLoading(false);
    }
  };

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value,
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!formData.title.trim() || !formData.content.trim()) {
      setError('Title and content are required');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      // Prepare the request body, including original_title for edits
      const requestBody = {
        ...formData,
        ...(isEdit && originalTitle ? { original_title: originalTitle } : {}),
      };

      const response = await fetch('/api/notes/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
      }

      onSuccess();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (initialLoading) {
    return <div>Loading note...</div>;
  }

  return (
    <div>
      <h1>{isEdit ? 'Edit Note' : 'Add New Note'}</h1>

      {error && <div style={{ color: 'red', marginBottom: '1rem' }}>Error: {error}</div>}

      <form onSubmit={handleSubmit} className="edit-form">
        <div>
          <label htmlFor="title">Title:</label>
          <input
            type="text"
            id="title"
            name="title"
            value={formData.title}
            onChange={handleChange}
            required
            disabled={loading}
          />
        </div>

        <div>
          <label htmlFor="content">Content:</label>
          <textarea
            id="content"
            name="content"
            value={formData.content}
            onChange={handleChange}
            required
            disabled={loading}
            rows={20}
            style={{ fontFamily: 'monospace' }}
          />
        </div>

        <div className="checkbox-container">
          <label>
            <input
              type="checkbox"
              name="include_in_prompt"
              checked={formData.include_in_prompt}
              onChange={handleChange}
              disabled={loading}
            />
            Include in system prompt
          </label>
          <small>
            When enabled, this note will be included in the system prompt for LLM conversations.
          </small>
        </div>

        <div>
          <Button type="submit" disabled={loading}>
            {loading ? 'Saving...' : 'Save'}
          </Button>
          <Button type="button" variant="secondary" onClick={onCancel} disabled={loading}>
            Cancel
          </Button>
        </div>
      </form>
    </div>
  );
};

export default NotesForm;
