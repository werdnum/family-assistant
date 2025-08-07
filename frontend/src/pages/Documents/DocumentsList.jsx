import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import styles from './Documents.module.css';

const DocumentsList = () => {
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [reindexing, setReindexing] = useState({});
  const limit = 20;
  const abortControllerRef = useRef(null);

  const fetchDocuments = useCallback(async (page = 1, abortSignal) => {
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

      const data = await response.json();
      setDocuments(data.documents);
      setTotal(data.total);
      setTotalPages(Math.ceil(data.total / limit));
      setCurrentPage(page);
    } catch (err) {
      // Don't log errors for aborted requests (happens during navigation)
      if (err.name !== 'AbortError' && !err.message?.includes('aborted')) {
        // Only log real errors, not navigation-related aborts
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

  const handleReindex = async (documentId) => {
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
      // Clear success message after 5 seconds
      window.setTimeout(() => setSuccess(null), 5000);
    } catch (err) {
      console.error('Error reindexing document:', err);
      setError(`Error: ${err.message}`);
      // Clear error message after 5 seconds
      window.setTimeout(() => setError(null), 5000);
    } finally {
      setReindexing((prev) => ({ ...prev, [documentId]: false }));
    }
  };

  const formatDate = (dateString) => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  const handlePageChange = (newPage) => {
    if (newPage >= 1 && newPage <= totalPages) {
      // Abort any previous request
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      // Create new abort controller for this page change
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

            {documents.map((doc) => (
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
                  <button
                    className={styles.reindexButton}
                    onClick={() => handleReindex(doc.id)}
                    disabled={reindexing[doc.id]}
                  >
                    {reindexing[doc.id] ? 'Reindexing...' : 'Reindex'}
                  </button>
                </div>
              </div>
            ))}
          </div>

          {totalPages > 1 && (
            <div className={styles.pagination}>
              <button
                onClick={() => handlePageChange(currentPage - 1)}
                disabled={currentPage === 1}
                className={styles.pageButton}
              >
                Previous
              </button>

              <span className={styles.pageInfo}>
                Page {currentPage} of {totalPages}
              </span>

              <button
                onClick={() => handlePageChange(currentPage + 1)}
                disabled={currentPage === totalPages}
                className={styles.pageButton}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default DocumentsList;
