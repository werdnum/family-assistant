import React, { useState, useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { ColumnDef } from '@tanstack/react-table';
import { Button } from '@/components/ui/button';
import { DataTable, SortableHeader } from '@/components/ui/data-table';

interface Note {
  title: string;
  content: string;
  include_in_prompt: boolean;
}

const NotesListWithDataTable = () => {
  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    fetchNotes(abortController.signal);

    return () => {
      abortController.abort();
    };
  }, []);

  const fetchNotes = async (signal: AbortSignal) => {
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
    } catch (err: unknown) {
      // Don't log errors for aborted requests (happens during navigation)
      if (err instanceof Error && err.name !== 'AbortError' && !err.message?.includes('aborted')) {
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

  const handleDelete = async (title: string) => {
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
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      setError(`Error deleting note: ${message}`);
    }
  };

  const columns: ColumnDef<Note>[] = [
    {
      accessorKey: 'title',
      header: ({ column }) => <SortableHeader column={column} title="Title" />,
      cell: ({ row }) => <div className="font-medium">{row.getValue('title')}</div>,
    },
    {
      accessorKey: 'include_in_prompt',
      header: ({ column }) => <SortableHeader column={column} title="Status" />,
      cell: ({ row }) => {
        const includeInPrompt = row.getValue('include_in_prompt') as boolean;
        return (
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
              includeInPrompt
                ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                : 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200'
            }`}
          >
            {includeInPrompt ? '✓ In Prompt' : '◯ Searchable'}
          </span>
        );
      },
    },
    {
      accessorKey: 'content',
      header: 'Content',
      cell: ({ row }) => (
        <div className="max-w-[200px] truncate text-sm text-muted-foreground">
          {row.getValue('content')}
        </div>
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: ({ row }) => {
        const note = row.original;
        return (
          <div className="flex items-center gap-2">
            <Link to={`/notes/edit/${encodeURIComponent(note.title)}`}>
              <Button size="sm" variant="outline">
                Edit
              </Button>
            </Link>
            <Button onClick={() => handleDelete(note.title)} variant="destructive" size="sm">
              Delete
            </Button>
          </div>
        );
      },
    },
  ];

  if (loading) {
    return <div className="flex items-center justify-center p-8">Loading notes...</div>;
  }

  if (error) {
    return (
      <div className="flex items-center justify-center p-8">
        <div className="text-destructive">Error loading notes: {error}</div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-bold tracking-tight">Notes</h1>
        <Link to="/notes/add">
          <Button>Add New Note</Button>
        </Link>
      </div>

      <DataTable
        columns={columns}
        data={notes}
        searchable={true}
        searchColumnId="title"
        searchPlaceholder="Search notes by title..."
        pageSize={10}
      />
    </div>
  );
};

export default NotesListWithDataTable;
