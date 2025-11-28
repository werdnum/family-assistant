import React, { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import styles from './Documents.module.css';

const DocumentDetail = () => {
  const { id } = useParams();
  const [document, setDocument] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [reindexing, setReindexing] = useState(false);

  // Use refs to store timeout IDs for cleanup
  const successTimeoutRef = useRef(null);
  const errorTimeoutRef = useRef(null);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (successTimeoutRef.current) {
        window.clearTimeout(successTimeoutRef.current);
      }
      if (errorTimeoutRef.current) {
        window.clearTimeout(errorTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    const fetchDocument = async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await fetch(`/api/documents/${id}`);

        if (!response.ok) {
          throw new Error(`Failed to fetch document: ${response.statusText}`);
        }

        const data = await response.json();
        setDocument(data);
      } catch (err) {
        console.error('Error fetching document:', err);
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (id) {
      fetchDocument();
    }
  }, [id]);

  const handleReindex = async () => {
    try {
      setReindexing(true);
      setError(null);
      setSuccess(null);

      const response = await fetch(`/api/documents/${id}/reindex`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error(`Failed to reindex document: ${response.statusText}`);
      }

      const data = await response.json();
      setSuccess(data.message || 'Re-indexing task enqueued');
      // Clear any existing timeout and set new one
      if (successTimeoutRef.current) {
        window.clearTimeout(successTimeoutRef.current);
      }
      successTimeoutRef.current = window.setTimeout(() => setSuccess(null), 5000);
    } catch (err) {
      console.error('Error reindexing document:', err);
      setError(`Error: ${err.message}`);
      // Clear any existing timeout and set new one
      if (errorTimeoutRef.current) {
        window.clearTimeout(errorTimeoutRef.current);
      }
      errorTimeoutRef.current = window.setTimeout(() => setError(null), 5000);
    } finally {
      setReindexing(false);
    }
  };

  const formatDate = (dateString) => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  const formatMetadata = (metadata) => {
    if (!metadata || Object.keys(metadata).length === 0) {
      return 'None';
    }
    return JSON.stringify(metadata, null, 2);
  };

  const renderEmbeddingContent = (content) => {
    if (!content) {
      return null;
    }

    // Truncate very long content for display
    const maxLength = 500;
    const truncated = content.length > maxLength;
    const displayContent = truncated ? content.substring(0, maxLength) + '...' : content;

    return (
      <div className={styles.embeddingContent}>
        <pre className={styles.contentPreview}>{displayContent}</pre>
        {truncated && (
          <div className={styles.contentTruncated}>
            Content truncated (showing first {maxLength} of {content.length} characters)
          </div>
        )}
      </div>
    );
  };

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <h1>Document Detail</h1>
          <Link to="/documents" className={styles.backButton}>
            Back to Documents
          </Link>
        </div>
        <div className={styles.loading}>Loading document...</div>
      </div>
    );
  }

  if (error && !document) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <h1>Document Detail</h1>
          <Link to="/documents" className={styles.backButton}>
            Back to Documents
          </Link>
        </div>
        <div className={styles.error}>Error: {error}</div>
      </div>
    );
  }

  if (!document) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <h1>Document Detail</h1>
          <Link to="/documents" className={styles.backButton}>
            Back to Documents
          </Link>
        </div>
        <div className={styles.empty}>Document not found.</div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Document Detail</h1>
        <Link to="/documents" className={styles.backButton}>
          Back to Documents
        </Link>
      </div>

      {error && <div className={styles.error}>Error: {error}</div>}
      {success && <div className={styles.success}>{success}</div>}

      {/* Document Metadata */}
      <div className={styles.detailSection}>
        <h2>Document Information</h2>
        <div className={styles.metadataGrid}>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Title:</span>
            <span className={styles.metadataValue}>{document.title || 'Untitled'}</span>
          </div>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Source Type:</span>
            <span className={styles.metadataValue}>{document.source_type}</span>
          </div>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Source ID:</span>
            <span className={styles.metadataValue}>
              <code className={styles.sourceId}>{document.source_id}</code>
            </span>
          </div>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Source URI:</span>
            <span className={styles.metadataValue}>
              {document.source_uri ? (
                <a
                  href={document.source_uri}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.sourceLink}
                >
                  {document.source_uri}
                </a>
              ) : (
                'N/A'
              )}
            </span>
          </div>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Created:</span>
            <span className={styles.metadataValue}>{formatDate(document.created_at)}</span>
          </div>
          <div className={styles.metadataRow}>
            <span className={styles.metadataLabel}>Added:</span>
            <span className={styles.metadataValue}>{formatDate(document.added_at)}</span>
          </div>
        </div>

        <div className={styles.actionButtons}>
          <Button onClick={handleReindex} disabled={reindexing}>
            {reindexing ? 'Reindexing...' : 'Reindex Document'}
          </Button>
        </div>
      </div>

      {/* Document Metadata JSON */}
      {document.doc_metadata && Object.keys(document.doc_metadata).length > 0 && (
        <div className={styles.detailSection}>
          <h2>Metadata</h2>
          <pre className={styles.metadataJson}>{formatMetadata(document.doc_metadata)}</pre>
        </div>
      )}

      {/* Full Text Content */}
      {document.full_text && (
        <div className={styles.detailSection}>
          <h2>Full Text Content</h2>
          {document.full_text_warning && (
            <div className={styles.warning}>{document.full_text_warning}</div>
          )}
          <div className={styles.contentTypeLabel}>
            Content Type: <code>{document.full_text_type}</code>
          </div>
          <div className={styles.fullTextContainer}>
            <pre className={styles.fullText}>{document.full_text}</pre>
          </div>
        </div>
      )}

      {/* Embeddings */}
      {document.embeddings && document.embeddings.length > 0 && (
        <div className={styles.detailSection}>
          <h2>Embeddings & Content Chunks</h2>
          <div className={styles.embeddingsStats}>
            Total embeddings: {document.embeddings.length}
          </div>

          <div className={styles.embeddingsList}>
            {document.embeddings.map((embedding) => (
              <div key={embedding.id} className={styles.embeddingItem}>
                <div className={styles.embeddingHeader}>
                  <div className={styles.embeddingType}>
                    <strong>Type:</strong> {embedding.embedding_type}
                  </div>
                  <div className={styles.embeddingModel}>
                    <strong>Model:</strong> {embedding.embedding_model}
                  </div>
                  {embedding.chunk_index !== null && (
                    <div className={styles.embeddingChunk}>
                      <strong>Chunk:</strong> {embedding.chunk_index}
                    </div>
                  )}
                </div>

                {embedding.metadata && Object.keys(embedding.metadata).length > 0 && (
                  <div className={styles.embeddingMetadata}>
                    <strong>Metadata:</strong>
                    <pre className={styles.embeddingMetadataJson}>
                      {JSON.stringify(embedding.metadata, null, 2)}
                    </pre>
                  </div>
                )}

                {embedding.content && renderEmbeddingContent(embedding.content)}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default DocumentDetail;
