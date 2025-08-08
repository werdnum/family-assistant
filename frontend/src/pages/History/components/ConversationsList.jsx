import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import HistoryFilters from './HistoryFilters';
import HistoryPagination from './HistoryPagination';
import styles from './ConversationsList.module.css';

const ConversationsList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [conversations, setConversations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(0);

  // Pagination settings
  const pageSize = 20;
  const currentPage = parseInt(searchParams.get('page') || '1');

  // Parse date with UTC to avoid timezone issues
  const parseUrlDate = (dateString) => {
    if (!dateString) {
      return null;
    }
    // Ensure consistent UTC interpretation across browsers
    return new Date(dateString + 'T00:00:00Z');
  };

  // Get current filter values from URL params (always in sync with URL)
  const filters = useMemo(
    () => ({
      interface_type: searchParams.get('interface_type') || '',
      conversation_id: searchParams.get('conversation_id') || '',
      date_from: parseUrlDate(searchParams.get('date_from')),
      date_to: parseUrlDate(searchParams.get('date_to')),
    }),
    [searchParams]
  );

  const fetchConversations = useCallback(
    async (page, currentFilters) => {
      setLoading(true);
      setError(null);

      try {
        const params = new URLSearchParams({
          limit: pageSize.toString(),
          offset: ((page - 1) * pageSize).toString(),
        });

        // Add filters to API call
        if (currentFilters.interface_type) {
          params.append('interface_type', currentFilters.interface_type);
        }
        if (currentFilters.conversation_id) {
          params.append('conversation_id', currentFilters.conversation_id);
        }
        if (currentFilters.date_from) {
          params.append('date_from', currentFilters.date_from.toISOString().split('T')[0]);
        }
        if (currentFilters.date_to) {
          params.append('date_to', currentFilters.date_to.toISOString().split('T')[0]);
        }

        const response = await fetch(`/api/v1/chat/conversations?${params}`);

        if (!response.ok) {
          throw new Error(`Failed to fetch conversations: ${response.statusText}`);
        }

        const data = await response.json();
        setConversations(data.conversations || []);
        setTotalCount(data.total || 0);
        setTotalPages(Math.ceil((data.total || 0) / pageSize));
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    [pageSize]
  );

  // Fetch data when page or filters change
  useEffect(() => {
    fetchConversations(currentPage, filters);
  }, [
    fetchConversations,
    currentPage,
    filters.interface_type,
    filters.conversation_id,
    filters.date_from,
    filters.date_to,
  ]);

  // Update filters and URL params
  const handleFiltersChange = (newFilters) => {
    // Update URL params (filters will be derived from URL automatically)
    const newParams = new URLSearchParams();

    if (newFilters.interface_type) {
      newParams.set('interface_type', newFilters.interface_type);
    }
    if (newFilters.conversation_id) {
      newParams.set('conversation_id', newFilters.conversation_id);
    }
    if (newFilters.date_from) {
      newParams.set('date_from', newFilters.date_from.toISOString().split('T')[0]);
    }
    if (newFilters.date_to) {
      newParams.set('date_to', newFilters.date_to.toISOString().split('T')[0]);
    }

    newParams.set('page', '1'); // Reset to first page when filtering
    setSearchParams(newParams);

    // useEffect will trigger fetchConversations when filters change
  };

  const handleClearFilters = () => {
    // Clear URL params (filters will be derived from URL automatically)
    setSearchParams({});
    // useEffect will trigger fetchConversations when filters change
  };

  const handlePageChange = (newPage) => {
    const newParams = new URLSearchParams(searchParams);
    newParams.set('page', newPage.toString());
    setSearchParams(newParams);
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown';
    }
    return new Date(timestamp).toLocaleString();
  };

  const formatConversationId = (id) => {
    // Truncate long conversation IDs for display in list
    if (id.length > 30) {
      return `${id.substring(0, 15)}...${id.substring(id.length - 15)}`;
    }
    return id;
  };

  const getInterfaceIcon = (interfaceType) => {
    switch (interfaceType) {
      case 'web':
        return '🌐';
      case 'telegram':
        return '📱';
      case 'api':
        return '🔗';
      case 'email':
        return '📧';
      default:
        return '💬';
    }
  };

  const truncateMessage = (message, maxLength = 100) => {
    if (!message) {
      return 'No content';
    }
    if (message.length <= maxLength) {
      return message;
    }
    return `${message.substring(0, maxLength)}...`;
  };

  if (loading && conversations.length === 0) {
    return (
      <div className={styles.conversationsList}>
        <h1>Conversation History</h1>
        <div className={styles.loading}>Loading conversations...</div>
      </div>
    );
  }

  return (
    <div className={styles.conversationsList}>
      <h1>Conversation History</h1>

      {/* Filters */}
      <HistoryFilters
        filters={filters}
        onFiltersChange={handleFiltersChange}
        onClearFilters={handleClearFilters}
        loading={loading}
      />

      {/* Results Summary */}
      <div className={styles.resultsInfo}>
        <p>
          Found {totalCount} conversation{totalCount !== 1 ? 's' : ''}
          {Object.values(filters).some((v) => v) && ' matching your criteria'}
        </p>
      </div>

      {error && <div className={styles.error}>Error: {error}</div>}

      {/* Conversations List */}
      {conversations.length > 0 ? (
        <>
          <div className={styles.conversationsContainer}>
            {conversations.map((conversation) => (
              <div key={conversation.conversation_id} className={styles.conversationCard}>
                <div className={styles.conversationHeader}>
                  <div className={styles.conversationTitle}>
                    <h3>
                      <Link
                        to={`/history/${encodeURIComponent(conversation.conversation_id)}`}
                        className={styles.conversationLink}
                      >
                        {formatConversationId(conversation.conversation_id)}
                      </Link>
                    </h3>
                  </div>
                  <span
                    className={styles.interfaceIcon}
                    title={`Interface: ${conversation.interface_type || 'unknown'}`}
                  >
                    {getInterfaceIcon(conversation.interface_type)}
                  </span>
                </div>

                <div className={styles.conversationPreview}>
                  <p className={styles.lastMessage}>{truncateMessage(conversation.last_message)}</p>
                </div>

                <div className={styles.conversationMeta}>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Messages:</span>
                    <span className={styles.metaValue}>{conversation.message_count}</span>
                  </div>
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Last Activity:</span>
                    <span className={styles.metaValue}>
                      {formatTimestamp(conversation.last_timestamp)}
                    </span>
                  </div>
                </div>

                <div className={styles.conversationFooter}>
                  <Link
                    to={`/history/${encodeURIComponent(conversation.conversation_id)}`}
                    className={styles.viewLink}
                  >
                    View Conversation →
                  </Link>
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          <HistoryPagination
            currentPage={currentPage}
            totalPages={totalPages}
            totalItems={totalCount}
            pageSize={pageSize}
            onPageChange={handlePageChange}
            loading={loading}
          />
        </>
      ) : !loading ? (
        <div className={styles.emptyState}>
          <p>No conversations found matching your criteria.</p>
          {Object.values(filters).some((v) => v) && (
            <Button onClick={handleClearFilters} variant="secondary">
              Clear Filters
            </Button>
          )}
        </div>
      ) : null}
    </div>
  );
};

export default ConversationsList;
