import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import styles from './VectorSearch.module.css';

const VectorSearch = () => {
  const [searchParams, setSearchParams] = useState({
    query_text: '',
    limit: 10,
    filters: {
      source_types: [],
      embedding_types: [],
      created_after: '',
      created_before: '',
      title_like: '',
      metadata_filters: {},
    },
  });

  const [advancedMode, setAdvancedMode] = useState(false);
  const [metadataFilterRows, setMetadataFilterRows] = useState([]);
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Available options fetched from the backend
  const [availableOptions, setAvailableOptions] = useState({
    models: [],
    types: [],
    source_types: [],
    metadata_keys: [],
  });

  // Fetch available filter options on mount
  useEffect(() => {
    const fetchOptions = async () => {
      try {
        // We need to get these from the documents API
        const docsResponse = await fetch('/api/documents/?limit=0');
        if (docsResponse.ok) {
          // This gives us a basic idea, but we might need a dedicated endpoint
          // For now, we'll use hardcoded common values
          setAvailableOptions({
            models: [], // Will be populated when we have a proper endpoint
            types: ['content_chunk', 'raw_note_text', 'raw_file_text'],
            source_types: ['manual_upload', 'manual_test_upload', 'api_generated'],
            metadata_keys: ['author', 'category', 'priority'],
          });
        }
      } catch (err) {
        console.error('Error fetching filter options:', err);
      }
    };

    fetchOptions();
  }, []);

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    if (name === 'query_text' || name === 'limit') {
      setSearchParams((prev) => ({ ...prev, [name]: value }));
    } else if (name.startsWith('filters.')) {
      const filterName = name.substring(8);
      setSearchParams((prev) => ({
        ...prev,
        filters: { ...prev.filters, [filterName]: value },
      }));
    }
  };

  const handleCheckboxChange = (category, value, checked) => {
    setSearchParams((prev) => {
      const currentValues = prev.filters[category] || [];
      const newValues = checked
        ? [...currentValues, value]
        : currentValues.filter((v) => v !== value);
      return {
        ...prev,
        filters: { ...prev.filters, [category]: newValues },
      };
    });
  };

  const addMetadataFilter = () => {
    setMetadataFilterRows((prev) => [...prev, { key: '', value: '', id: Date.now() }]);
  };

  const updateMetadataFilter = (id, field, value) => {
    setMetadataFilterRows((prev) =>
      prev.map((row) => (row.id === id ? { ...row, [field]: value } : row))
    );
  };

  const removeMetadataFilter = (id) => {
    setMetadataFilterRows((prev) => prev.filter((row) => row.id !== id));
  };

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!searchParams.query_text.trim()) {
      setError('Please enter a search query');
      return;
    }

    setLoading(true);
    setError(null);
    setResults(null);

    try {
      // Build metadata filters object from rows
      const metadataFilters = {};
      metadataFilterRows.forEach((row) => {
        if (row.key && row.value) {
          metadataFilters[row.key] = row.value;
        }
      });

      // Build request payload
      const payload = {
        query_text: searchParams.query_text,
        limit: parseInt(searchParams.limit) || 10,
        filters: {
          source_types: searchParams.filters.source_types,
          embedding_types: searchParams.filters.embedding_types,
          created_after: searchParams.filters.created_after
            ? new Date(searchParams.filters.created_after).toISOString()
            : null,
          created_before: searchParams.filters.created_before
            ? new Date(searchParams.filters.created_before).toISOString()
            : null,
          title_like: searchParams.filters.title_like || null,
          metadata_filters: metadataFilters,
        },
      };

      // Remove empty arrays and null values from filters
      Object.keys(payload.filters).forEach((key) => {
        if (Array.isArray(payload.filters[key]) && payload.filters[key].length === 0) {
          delete payload.filters[key];
        } else if (payload.filters[key] === null || payload.filters[key] === '') {
          delete payload.filters[key];
        }
      });

      const response = await fetch('/api/vector-search/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Search failed');
      }

      const data = await response.json();
      setResults(data);
    } catch (err) {
      console.error('Search error:', err);
      setError(err.message || 'An error occurred during search');
    } finally {
      setLoading(false);
    }
  };

  const formatMetadata = (metadata) => {
    if (!metadata || Object.keys(metadata).length === 0) {
      return 'None';
    }
    return JSON.stringify(metadata, null, 2);
  };

  const formatDate = (dateString) => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Vector Search</h1>
      </div>

      <form className={styles.searchForm} onSubmit={handleSearch}>
        <div className={styles.formGrid}>
          <label htmlFor="query_text">Search Query:</label>
          <textarea
            id="query_text"
            name="query_text"
            className={styles.searchInput}
            placeholder="Describe what you're looking for..."
            value={searchParams.query_text}
            onChange={handleInputChange}
            rows={3}
          />

          <label htmlFor="limit">Result Limit:</label>
          <input
            type="number"
            id="limit"
            name="limit"
            className={styles.limitInput}
            value={searchParams.limit}
            onChange={handleInputChange}
            min="1"
            max="100"
          />

          <label className={styles.fullWidth}>Filter by Source Type:</label>
          <div className={`${styles.checkboxGroup} ${styles.fullWidth}`}>
            {availableOptions.source_types.map((type) => (
              <label key={type}>
                <input
                  type="checkbox"
                  checked={searchParams.filters.source_types.includes(type)}
                  onChange={(e) => handleCheckboxChange('source_types', type, e.target.checked)}
                />
                {type}
              </label>
            ))}
          </div>

          <label htmlFor="title_like">Title Contains:</label>
          <input
            type="text"
            id="title_like"
            name="filters.title_like"
            className={styles.textInput}
            placeholder="Filter by title..."
            value={searchParams.filters.title_like}
            onChange={handleInputChange}
          />

          <label htmlFor="created_after">Created After:</label>
          <input
            type="date"
            id="created_after"
            name="filters.created_after"
            className={styles.dateInput}
            value={searchParams.filters.created_after}
            onChange={handleInputChange}
          />

          <label htmlFor="created_before">Created Before:</label>
          <input
            type="date"
            id="created_before"
            name="filters.created_before"
            className={styles.dateInput}
            value={searchParams.filters.created_before}
            onChange={handleInputChange}
          />

          <div className={styles.fullWidth}>
            <details className={styles.advancedOptions} open={advancedMode}>
              <summary onClick={() => setAdvancedMode(!advancedMode)}>Advanced Options</summary>
              <div className={styles.advancedContent}>
                <div className={styles.formGrid}>
                  <label className={styles.fullWidth}>Filter by Embedding Type:</label>
                  <div className={`${styles.checkboxGroup} ${styles.fullWidth}`}>
                    {availableOptions.types.map((type) => (
                      <label key={type}>
                        <input
                          type="checkbox"
                          checked={searchParams.filters.embedding_types.includes(type)}
                          onChange={(e) =>
                            handleCheckboxChange('embedding_types', type, e.target.checked)
                          }
                        />
                        {type}
                      </label>
                    ))}
                  </div>

                  <label className={styles.fullWidth}>Metadata Filters:</label>
                  <div className={`${styles.metadataFilters} ${styles.fullWidth}`}>
                    {metadataFilterRows.map((row) => (
                      <div key={row.id} className={styles.filterRow}>
                        <select
                          className={styles.filterSelect}
                          value={row.key}
                          onChange={(e) => updateMetadataFilter(row.id, 'key', e.target.value)}
                        >
                          <option value="">-- Select Key --</option>
                          {availableOptions.metadata_keys.map((key) => (
                            <option key={key} value={key}>
                              {key}
                            </option>
                          ))}
                        </select>
                        <input
                          type="text"
                          className={styles.filterInput}
                          placeholder="Value"
                          value={row.value}
                          onChange={(e) => updateMetadataFilter(row.id, 'value', e.target.value)}
                        />
                        <button
                          type="button"
                          className={styles.removeFilterBtn}
                          onClick={() => removeMetadataFilter(row.id)}
                        >
                          Remove
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className={styles.addFilterBtn}
                      onClick={addMetadataFilter}
                    >
                      Add Metadata Filter
                    </button>
                  </div>
                </div>
              </div>
            </details>
          </div>

          <button
            type="submit"
            className={`${styles.searchButton} ${styles.fullWidth}`}
            disabled={loading}
          >
            {loading ? 'Searching...' : 'Search'}
          </button>
        </div>
      </form>

      {error && <div className={styles.error}>Error: {error}</div>}

      {loading && <div className={styles.loading}>Searching...</div>}

      {results !== null && !loading && (
        <div className={styles.results}>
          <h2 className={styles.resultsHeader}>
            Results {results.length > 0 && `(${results.length})`}
          </h2>
          {results.length === 0 ? (
            <div className={styles.noResults}>No results found for your search.</div>
          ) : (
            <div>
              {results.map((result) => (
                <article key={result.document.id} className={styles.resultCard}>
                  <h3 className={styles.resultTitle}>
                    {result.document.source_uri ? (
                      <a
                        href={result.document.source_uri}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {result.document.title || 'Untitled Document'}
                      </a>
                    ) : (
                      result.document.title || 'Untitled Document'
                    )}
                    <span className={styles.score}>Score: {result.score.toFixed(4)}</span>
                  </h3>

                  <div className={styles.sourceInfo}>
                    Source: {result.document.source_type} | Created:{' '}
                    {formatDate(result.document.created_at)}
                    <br />
                    <Link to={`/documents/${result.document.id}`} className={styles.documentLink}>
                      View Full Document Details (ID: {result.document.id})
                    </Link>
                  </div>

                  {result.document.metadata && Object.keys(result.document.metadata).length > 0 && (
                    <details className={styles.metadataSection}>
                      <summary>Document Metadata</summary>
                      <div className={styles.metadataContent}>
                        {formatMetadata(result.document.metadata)}
                      </div>
                    </details>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default VectorSearch;
