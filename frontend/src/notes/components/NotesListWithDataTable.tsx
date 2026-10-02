import { ColumnDef } from '@tanstack/react-table';
import { PaperclipIcon } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { DataTable, SortableHeader } from '@/components/ui/data-table';

interface Note {
  title: string;
  content: string;
  include_in_prompt: boolean;
  attachment_ids?: string[];
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
      cell: ({ row }) => (
        <Link
          to={`/notes/edit/${encodeURIComponent(row.original.title)}`}
          className="font-medium text-primary hover:underline break-words"
        >
          {row.getValue('title')}
        </Link>
      ),
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
      accessorKey: 'attachment_ids',
      header: ({ column }) => <SortableHeader column={column} title="Attachments" />,
      cell: ({ row }) => {
        const attachmentIds = (row.getValue('attachment_ids') as string[]) || [];
        const count = attachmentIds.length;
        if (count === 0) {
          return <span className="text-sm text-muted-foreground">-</span>;
        }
        return (
          <div className="flex items-center gap-1">
            <PaperclipIcon className="size-4 text-muted-foreground" />
            <span className="text-sm font-medium">{count}</span>
          </div>
        );
      },
      sortingFn: (rowA, rowB) => {
        const countA = ((rowA.getValue('attachment_ids') as string[]) || []).length;
        const countB = ((rowB.getValue('attachment_ids') as string[]) || []).length;
        return countA - countB;
      },
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: ({ row }) => {
        const note = row.original;
        return (
          <div className="flex items-center gap-2">
            <Button asChild size="sm" variant="outline">
              <Link to={`/notes/edit/${encodeURIComponent(note.title)}`}>Edit</Link>
            </Button>
            <Button onClick={() => handleDelete(note.title)} variant="destructive" size="sm">
              Delete
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="container mx-auto py-6">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <h1 className="text-3xl font-bold tracking-tight">Notes</h1>
        <Button asChild>
          <Link to="/notes/add">Add New Note</Link>
        </Button>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {loading ? (
        <div role="status" className="p-8 text-center text-muted-foreground">
          Loading notes...
        </div>
      ) : error && notes.length === 0 ? (
        <Button
          variant="outline"
          onClick={() => {
            const controller = new AbortController();
            abortControllerRef.current = controller;
            fetchNotes(controller.signal);
          }}
        >
          Try again
        </Button>
      ) : (
        <DataTable
          columns={columns}
          data={notes}
          searchable={true}
          searchColumnId="title"
          searchPlaceholder="Search notes by title..."
          pageSize={10}
          emptyStateMessage="No notes found"
        />
      )}
    </div>
  );
};

export default NotesListWithDataTable;
