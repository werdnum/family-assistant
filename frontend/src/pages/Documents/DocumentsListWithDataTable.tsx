import { ColumnDef } from '@tanstack/react-table';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { DataTable, SortableHeader } from '@/components/ui/data-table';

interface Document {
  id: number;
  title: string;
  source_type: string;
  source_id: string;
  source_uri?: string;
  created_at: string;
  added_at: string;
}

const DocumentsListWithDataTable = () => {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [reindexing, setReindexing] = useState<Record<number, boolean>>({});
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    fetchDocuments(abortController.signal);

    return () => {
      abortController.abort();
    };
  }, []);

  const fetchDocuments = async (signal: AbortSignal) => {
    try {
      setLoading(true);
      setError(null);
      // Fetch all documents at once for DataTable client-side pagination
      const response = await fetch('/api/documents/?limit=1000&offset=0', { signal });

      if (!response.ok) {
        throw new Error(`Failed to fetch documents: ${response.statusText}`);
      }

      const data = await response.json();
      setDocuments(data.documents || []);
    } catch (err: unknown) {
      // Don't log errors for aborted requests (happens during navigation)
      if (err instanceof Error && err.name !== 'AbortError' && !err.message?.includes('aborted')) {
        const message =
          err instanceof TypeError && err.message.includes('Failed to fetch')
            ? 'Could not connect to the API server. Please check your network connection and if the server is running.'
            : err.message;
        setError(message);
        // Log all actual errors for debugging
        console.error('Error fetching documents:', message);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleReindex = async (documentId: number) => {
    try {
      setReindexing((prev) => ({ ...prev, [documentId]: true }));
      setError(null);
      setSuccess(null);

      const response = await fetch(`/api/documents/${documentId}/reindex`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error(`Failed to reindex document: ${response.statusText}`);
      }

      const data = await response.json();
      setSuccess(data.message || 'Re-indexing task enqueued');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      console.error('Error reindexing document:', err);
      setError(`Error: ${message}`);
    } finally {
      setReindexing((prev) => ({ ...prev, [documentId]: false }));
    }
  };

  const formatDate = (dateString: string): string => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  const columns: ColumnDef<Document>[] = [
    {
      accessorKey: 'title',
      header: ({ column }) => <SortableHeader column={column} title="Title" />,
      cell: ({ row }) => {
        const doc = row.original;
        return (
          <div className="space-y-1">
            <Link to={`/documents/${doc.id}`} className="font-medium text-primary hover:underline">
              {doc.title || 'Untitled'}
            </Link>
            {doc.source_uri && (
              <div className="text-xs text-muted-foreground truncate max-w-[200px]">
                {doc.source_uri}
              </div>
            )}
          </div>
        );
      },
    },
    {
      accessorKey: 'source_type',
      header: ({ column }) => <SortableHeader column={column} title="Type" />,
      cell: ({ row }) => (
        <Badge variant="secondary" className="text-xs">
          {row.getValue('source_type')}
        </Badge>
      ),
    },
    {
      accessorKey: 'source_id',
      header: 'Source ID',
      cell: ({ row }) => (
        <code className="text-xs bg-muted px-1 py-0.5 rounded">{row.getValue('source_id')}</code>
      ),
    },
    {
      accessorKey: 'created_at',
      header: ({ column }) => <SortableHeader column={column} title="Created" />,
      cell: ({ row }) => (
        <div className="text-sm text-muted-foreground">
          {formatDate(row.getValue('created_at'))}
        </div>
      ),
    },
    {
      accessorKey: 'added_at',
      header: ({ column }) => <SortableHeader column={column} title="Added" />,
      cell: ({ row }) => (
        <div className="text-sm text-muted-foreground">{formatDate(row.getValue('added_at'))}</div>
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: ({ row }) => {
        const doc = row.original;
        return (
          <Button
            size="sm"
            variant="outline"
            onClick={() => handleReindex(doc.id)}
            disabled={reindexing[doc.id]}
          >
            {reindexing[doc.id] ? 'Reindexing...' : 'Reindex'}
          </Button>
        );
      },
    },
  ];

  if (loading) {
    return <div className="flex items-center justify-center p-8">Loading documents...</div>;
  }

  if (error && documents.length === 0) {
    return (
      <div className="flex items-center justify-center p-8">
        <div className="text-destructive">Error loading documents: {error}</div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-bold tracking-tight">Documents</h1>
        <Link to="/documents/upload">
          <Button>Upload Document</Button>
        </Link>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-md bg-destructive/10 text-destructive text-sm">
          Error: {error}
        </div>
      )}

      {success && (
        <div className="mb-4 p-3 rounded-md bg-primary/10 text-primary text-sm">{success}</div>
      )}

      <DataTable
        columns={columns}
        data={documents}
        searchable={true}
        searchColumnId="title"
        searchPlaceholder="Search documents by title..."
        pageSize={20}
        emptyStateMessage="No documents found"
      />
    </div>
  );
};

export default DocumentsListWithDataTable;
