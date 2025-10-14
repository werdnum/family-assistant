import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import styles from './Documents.module.css';

interface ApiDocument {
  id: string;
  title: string | null;
  source_uri: string | null;
  source_type: string;
  source_id: string;
  created_at: string;
  added_at: string;
}

interface FetchResponse {
  documents: ApiDocument[];
  total: number;
}

const DocumentsList: React.FC = () => {
  const [documents, setDocuments] = useState<ApiDocument[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [totalPages, setTotalPages] = useState<number>(1);
  const [total, setTotal] = useState<number>(0);
  const [reindexing, setReindexing] = useState<Record<string, boolean>>({});
  const limit = 20;
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchDocuments = useCallback(async (page = 1, abortSignal: AbortSignal) => {
    try {
      setLoading(true);
      setError(null);
      const offset = (page - 1) * limit;
      const response = await fetch(`/api/documents/?limit=${limit}&offset=${offset}`, {
        signal: abortSignal,
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch documents: ${response.statusText}`);
      }

      const data: FetchResponse = await response.json();
      setDocuments(data.documents);
      setTotal(data.total);
      setTotalPages(Math.ceil(data.total / limit));
      setCurrentPage(page);
    } catch (err: any) {
      if (err.name !== 'AbortError' && !err.message?.includes('aborted')) {
        if (!err.message?.includes('Failed to fetch')) {
          console.error('Error fetching documents:', err);
        }
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    fetchDocuments(1, abortController.signal);

    return () => {
      abortController.abort();
    };
  }, [fetchDocuments]);

  const handleReindex = async (documentId: string) => {
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
      window.setTimeout(() => setSuccess(null), 5000);
    } catch (err: any) {
      console.error('Error reindexing document:', err);
      setError(`Error: ${err.message}`);
      window.setTimeout(() => setError(null), 5000);
    } finally {
      setReindexing((prev) => ({ ...prev, [documentId]: false }));
    }
  };

  const formatDate = (dateString: string | null): string => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  const handlePageChange = (newPage: number) => {
    if (newPage >= 1 && newPage <= totalPages) {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      const abortController = new AbortController();
      abortControllerRef.current = abortController;
      fetchDocuments(newPage, abortController.signal);
    }
  };

  if (loading && documents.length === 0) {
    return (
      <div className={styles.container}>
        <h1>Documents</h1>
        <div className={styles.loading}>Loading documents...</div>
      </div>
    );
  }

  if (error && documents.length === 0) {
    return (
      <div className={styles.container}>
        <h1>Documents</h1>
        <div className={styles.error}>Error: {error}</div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Documents</h1>
        <Link to="/documents/upload" className={styles.uploadButton}>
          Upload Document
        </Link>
      </div>

      {error && <div className={styles.error}>Error: {error}</div>}
      {success && <div className={styles.success}>{success}</div>}

      <div className={styles.stats}>Total documents: {total}</div>

      {documents.length === 0 ? (
        <div className={styles.empty}>No documents found.</div>
      ) : (
        <>
          <div className={styles.documentsTable}>
            <div className={styles.tableHeader}>
              <div className={styles.headerCell}>Title</div>
              <div className={styles.headerCell}>Type</div>
              <div className={styles.headerCell}>Source ID</div>
              <div className={styles.headerCell}>Created</div>
              <div className={styles.headerCell}>Added</div>
              <div className={styles.headerCell}>Actions</div>
            </div>

            {documents.map((doc: ApiDocument) => (
              <div key={doc.id} className={styles.tableRow}>
                <div className={styles.cell}>
                  <Link to={`/documents/${doc.id}`} className={styles.documentLink}>
                    {doc.title || 'Untitled'}
                  </Link>
                  {doc.source_uri && (
                    <div className={styles.sourceUri}>
                      <small>{doc.source_uri}</small>
                    </div>
                  )}
                </div>
                <div className={styles.cell}>{doc.source_type}</div>
                <div className={styles.cell}>
                  <code className={styles.sourceId}>{doc.source_id}</code>
                </div>
                <div className={styles.cell}>{formatDate(doc.created_at)}</div>
                <div className={styles.cell}>{formatDate(doc.added_at)}</div>
                <div className={styles.cell}>
                  <Button
                    size="sm"
                    onClick={() => handleReindex(doc.id)}
                    disabled={reindexing[doc.id]}
                  >
                    {reindexing[doc.id] ? 'Reindexing...' : 'Reindex'}
                  </Button>
                </div>
              </div>
            ))}
          </div>

          {totalPages > 1 && (
            <div className={styles.pagination}>
              <Button
                onClick={() => handlePageChange(currentPage - 1)}
                disabled={currentPage === 1}
                variant="outline"
                size="sm"
              >
                Previous
              </Button>

              <span className={styles.pageInfo}>
                Page {currentPage} of {totalPages}
              </span>

              <Button
                onClick={() => handlePageChange(currentPage + 1)}
                disabled={currentPage === totalPages}
                variant="outline"
                size="sm"
              >
                Next
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default DocumentsList;
