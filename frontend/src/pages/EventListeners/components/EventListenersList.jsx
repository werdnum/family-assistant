import React, { useState, useEffect } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import styles from './EventListenersList.module.css';
import './EventListenersList.css';

const EventListenersList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [listeners, setListeners] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(0);

  // Get current filter values from URL params
  const currentPage = parseInt(searchParams.get('page') || '1');
  const currentSourceId = searchParams.get('source_id') || '';
  const currentActionType = searchParams.get('action_type') || '';
  const currentConversationId = searchParams.get('conversation_id') || '';
  const currentEnabled = searchParams.get('enabled') || '';

  // Form state for filters
  const [filters, setFilters] = useState({
    source_id: currentSourceId,
    action_type: currentActionType,
    conversation_id: currentConversationId,
    enabled: currentEnabled,
  });

  const fetchListeners = async (page, sourceId, actionType, conversationId, enabled) => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        page: page.toString(),
        page_size: '50',
      });

      if (sourceId) {
        params.append('source_id', sourceId);
      }
      if (actionType) {
        params.append('action_type', actionType);
      }
      if (conversationId) {
        params.append('conversation_id', conversationId);
      }
      if (enabled) {
        params.append('enabled', enabled);
      }

      const response = await fetch(`/api/event-listeners?${params}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch event listeners: ${response.statusText}`);
      }

      const data = await response.json();
      setListeners(data.listeners);
      setTotalCount(data.total_count);
      setTotalPages(Math.ceil(data.total_count / data.page_size));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Fetch data when filters change
  useEffect(() => {
    fetchListeners(
      currentPage,
      currentSourceId,
      currentActionType,
      currentConversationId,
      currentEnabled
    );
  }, [currentPage, currentSourceId, currentActionType, currentConversationId, currentEnabled]);

  const handleFiltersSubmit = (e) => {
    e.preventDefault();

    // Update URL params
    const newParams = new URLSearchParams();
    if (filters.source_id) {
      newParams.set('source_id', filters.source_id);
    }
    if (filters.action_type) {
      newParams.set('action_type', filters.action_type);
    }
    if (filters.conversation_id) {
      newParams.set('conversation_id', filters.conversation_id);
    }
    if (filters.enabled) {
      newParams.set('enabled', filters.enabled);
    }
    newParams.set('page', '1'); // Reset to first page when filtering

    setSearchParams(newParams);
  };

  const clearFilters = () => {
    setFilters({
      source_id: '',
      action_type: '',
      conversation_id: '',
      enabled: '',
    });
    setSearchParams({});
  };

  const handlePageChange = (newPage) => {
    const newParams = new URLSearchParams(searchParams);
    newParams.set('page', newPage.toString());
    setSearchParams(newParams);
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Never';
    }
    return new Date(timestamp).toLocaleString();
  };

  const getActionIcon = (actionType) => {
    return actionType === 'wake_llm' ? '🤖' : '📜';
  };

  const getActionTitle = (actionType) => {
    return actionType === 'wake_llm' ? 'LLM Callback' : 'Script Execution';
  };

  const formatSourceId = (sourceId) => {
    return sourceId.replace('_', ' ').replace(/\b\w/g, (l) => l.toUpperCase());
  };

  if (loading) {
    return <div>Loading event listeners...</div>;
  }
  if (error) {
    return <div className={styles.error}>Error: {error}</div>;
  }

  return (
    <div className={styles.eventListenersList}>
      <h1>Event Listeners</h1>

      <div style={{ marginBottom: '1rem' }}>
        <Link to="/event-listeners/new" className="button">
          + Create New Listener
        </Link>
      </div>

      {/* Filters Form */}
      <form onSubmit={handleFiltersSubmit} style={{ marginBottom: '2rem' }}>
        <details open>
          <summary>Filters</summary>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: '1rem',
              marginTop: '1rem',
            }}
          >
            <div>
              <label htmlFor="source_id">Event Source:</label>
              <select
                name="source_id"
                id="source_id"
                value={filters.source_id}
                onChange={(e) => setFilters({ ...filters, source_id: e.target.value })}
              >
                <option value="">All Sources</option>
                <option value="home_assistant">Home Assistant</option>
                <option value="indexing">Document Indexing</option>
                <option value="webhook">Webhook</option>
              </select>
            </div>

            <div>
              <label htmlFor="action_type">Action Type:</label>
              <select
                name="action_type"
                id="action_type"
                value={filters.action_type}
                onChange={(e) => setFilters({ ...filters, action_type: e.target.value })}
              >
                <option value="">All Types</option>
                <option value="wake_llm">LLM Callback</option>
                <option value="script">Script</option>
              </select>
            </div>

            <div>
              <label htmlFor="enabled">Status:</label>
              <select
                name="enabled"
                id="enabled"
                value={filters.enabled}
                onChange={(e) => setFilters({ ...filters, enabled: e.target.value })}
              >
                <option value="">All</option>
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>

            <div>
              <label htmlFor="conversation_id">Conversation ID:</label>
              <input
                type="text"
                name="conversation_id"
                id="conversation_id"
                value={filters.conversation_id}
                onChange={(e) => setFilters({ ...filters, conversation_id: e.target.value })}
                placeholder="Filter by conversation ID"
              />
            </div>
          </div>

          <div style={{ marginTop: '1rem' }}>
            <Button type="submit">Apply Filters</Button>
            <Button type="button" variant="secondary" onClick={clearFilters}>
              Clear Filters
            </Button>
          </div>
        </details>
      </form>

      {/* Results Summary */}
      <p>
        Found {totalCount} listener{totalCount !== 1 ? 's' : ''}
      </p>

      {/* Listeners List */}
      {listeners.length > 0 ? (
        <>
          <div className={styles.listenersContainer}>
            {listeners.map((listener) => (
              <div key={listener.id} className={styles.listenerCard}>
                <div className={styles.listenerHeader}>
                  <div className={styles.listenerTitle}>
                    <h3>
                      <Link to={`/event-listeners/${listener.id}`}>{listener.name}</Link>
                    </h3>
                    {listener.description && (
                      <p className={styles.listenerDescription}>
                        {listener.description.length > 100
                          ? `${listener.description.substring(0, 100)}...`
                          : listener.description}
                      </p>
                    )}
                  </div>
                  <span
                    className={styles.listenerActionType}
                    title={getActionTitle(listener.action_type)}
                  >
                    {getActionIcon(listener.action_type)}
                  </span>
                </div>

                <div className={styles.listenerMeta}>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Source:</span>
                    <span className={styles.metaValue}>{formatSourceId(listener.source_id)}</span>
                  </div>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Conversation:</span>
                    <span className={styles.metaValue}>{listener.conversation_id || 'None'}</span>
                  </div>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Status:</span>
                    {listener.enabled ? (
                      <span className={`${styles.statusBadge} ${styles.enabled}`}>✓ Enabled</span>
                    ) : (
                      <span className={`${styles.statusBadge} ${styles.disabled}`}>✗ Disabled</span>
                    )}
                    {listener.one_time && (
                      <span className={`${styles.statusBadge} ${styles.oneTime}`}>One-time</span>
                    )}
                  </div>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Executions:</span>
                    <span className={styles.metaValue}>
                      {listener.daily_executions || 0} / 5 today
                      {(listener.daily_executions || 0) >= 5 && (
                        <span className={styles.rateLimited}>(Rate limited)</span>
                      )}
                    </span>
                  </div>
                </div>

                <div className={styles.listenerFooter}>
                  <div className={styles.listenerTimestamps}>
                    <span className={styles.timestamp}>
                      <strong>Last triggered:</strong> {formatTimestamp(listener.last_execution_at)}
                    </span>
                    <span className={styles.timestamp}>
                      <strong>Created:</strong> {formatTimestamp(listener.created_at)}
                    </span>
                  </div>
                  <Link to={`/event-listeners/${listener.id}`} className={styles.viewLink}>
                    View Details →
                  </Link>
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <nav style={{ marginTop: '2rem', textAlign: 'center' }}>
              {currentPage > 1 && (
                <Button
                  onClick={() => handlePageChange(currentPage - 1)}
                  variant="outline"
                  size="sm"
                >
                  ← Previous
                </Button>
              )}

              <span style={{ margin: '0 1rem' }}>
                Page {currentPage} of {totalPages}
              </span>

              {currentPage < totalPages && (
                <Button
                  onClick={() => handlePageChange(currentPage + 1)}
                  variant="outline"
                  size="sm"
                >
                  Next →
                </Button>
              )}
            </nav>
          )}
        </>
      ) : (
        <p>No event listeners found matching your criteria.</p>
      )}
    </div>
  );
};

export default EventListenersList;
