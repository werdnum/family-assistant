import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import styles from './DocumentationList.module.css';

const DocumentationList = () => {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchDocs = async () => {
      try {
        setLoading(true);
        setError(null);

        const response = await fetch('/api/documentation/');
        if (!response.ok) {
          throw new Error(`Failed to fetch documentation list: ${response.statusText}`);
        }

        const docsData = await response.json();
        setDocs(docsData);
      } catch (err) {
        console.error('Error fetching docs:', err);
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    fetchDocs();
  }, []);

  if (loading) {
    return (
      <div className={styles.container}>
        <h1>Documentation</h1>
        <div className={styles.loading}>Loading documentation...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <h1>Documentation</h1>
        <div className={styles.error}>Error loading documentation: {error}</div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <h1>Documentation</h1>

      {docs.length === 0 ? (
        <div className={styles.empty}>No documentation files found.</div>
      ) : (
        <div className={styles.docsList}>
          {docs.map((filename) => (
            <Link
              key={filename}
              to={`/docs/${encodeURIComponent(filename)}`}
              className={styles.docItem}
            >
              <div className={styles.docTitle}>
                {filename.replace(/\.md$/, '').replace(/_/g, ' ')}
              </div>
              <div className={styles.docFilename}>{filename}</div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
};

export default DocumentationList;
