import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import MessageDisplay from './MessageDisplay';
import styles from './ConversationView.module.css';

const ConversationView = ({ onBackToList }) => {
  const { conversationId } = useParams();
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [totalMessages, setTotalMessages] = useState(0);
  const [hasMoreBefore, setHasMoreBefore] = useState(false);
  const [hasMoreAfter, setHasMoreAfter] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const hasInitiallyLoadedRef = useRef(false);

  const fetchMessages = useCallback(
    async (before = null, after = null, append = false) => {
      if (!append) {
        setLoading(true);
        setError(null);
      } else {
        setLoadingMore(true);
      }

      try {
        // Build query parameters
        const params = new URLSearchParams();
        params.set('limit', '50'); // Load 50 messages at a time

        if (before) {
          params.set('before', before);
        }
        if (after) {
          params.set('after', after);
        }

        const response = await fetch(
          `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages?${params}`
        );

        if (!response.ok) {
          throw new Error(`Failed to fetch messages: ${response.statusText}`);
        }

        const data = await response.json();
        const newMessages = data.messages || [];

        if (append) {
          if (before) {
            // Prepend older messages
            setMessages((prev) => [...newMessages, ...prev]);
          } else if (after) {
            // Append newer messages
            setMessages((prev) => [...prev, ...newMessages]);
          }
        } else {
          // Initial load or replace
          setMessages(newMessages);
        }

        setTotalMessages(data.total_messages || 0);
        setHasMoreBefore(data.has_more_before || false);
        setHasMoreAfter(data.has_more_after || false);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [conversationId]
  );

  useEffect(() => {
    if (conversationId) {
      fetchMessages();
    }
  }, [conversationId, fetchMessages]);

  // Auto-scroll to bottom on initial load only
  useEffect(() => {
    if (!loading && messages.length > 0 && !hasInitiallyLoadedRef.current) {
      hasInitiallyLoadedRef.current = true;
      // Scroll to bottom with a small delay to ensure DOM is updated
      setTimeout(() => {
        window.scrollTo(0, document.body.scrollHeight);
      }, 100);
    }
  }, [loading, messages.length]);

  // Load more messages before current batch
  const loadMoreBefore = useCallback(() => {
    if (messages.length === 0 || loadingMore) {
      return;
    }

    const oldestMessage = messages[0];
    const beforeTimestamp = oldestMessage.timestamp;
    fetchMessages(beforeTimestamp, null, true);
  }, [messages, loadingMore, fetchMessages]);

  // Load more messages after current batch
  const loadMoreAfter = useCallback(() => {
    if (messages.length === 0 || loadingMore) {
      return;
    }

    const newestMessage = messages[messages.length - 1];
    const afterTimestamp = newestMessage.timestamp;
    fetchMessages(null, afterTimestamp, true);
  }, [messages, loadingMore, fetchMessages]);

  // Jump to latest messages
  const jumpToLatest = useCallback(() => {
    fetchMessages(); // Reload from the beginning (most recent)
  }, [fetchMessages]);

  const groupMessagesByTurn = (messages) => {
    const turns = [];
    let currentTurn = [];

    for (const message of messages) {
      // Messages with the same turn_id belong to the same turn
      // For now, we'll group consecutive messages from the same role or related tool calls
      // Turn grouping logic:
      // 1. Start new turn if no messages yet
      // 2. Start new turn when user sends message after non-user messages
      // 3. Start new turn when assistant responds with content after tool interactions
      const isNewTurn =
        currentTurn.length === 0 ||
        (message.role === 'user' && currentTurn[currentTurn.length - 1].role !== 'user') ||
        (message.role === 'assistant' &&
          !message.tool_calls &&
          currentTurn.some((m) => m.role === 'tool' || (m.role === 'assistant' && m.tool_calls)));

      if (isNewTurn && currentTurn.length > 0) {
        turns.push([...currentTurn]);
        currentTurn = [];
      }

      currentTurn.push(message);
    }

    if (currentTurn.length > 0) {
      turns.push(currentTurn);
    }

    return turns;
  };

  const formatConversationId = (id) => {
    // Truncate long conversation IDs for display
    if (id.length > 40) {
      return `${id.substring(0, 20)}...${id.substring(id.length - 20)}`;
    }
    return id;
  };

  if (loading) {
    return (
      <div className={styles.conversationView}>
        <div className={styles.loading}>Loading conversation...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.conversationView}>
        <div className={styles.header}>
          <Button onClick={onBackToList} variant="outline">
            ← Back to Conversations
          </Button>
        </div>
        <div className={styles.error}>Error: {error}</div>
      </div>
    );
  }

  const messageTurns = groupMessagesByTurn(messages);

  return (
    <div className={styles.conversationView}>
      <div className={styles.header}>
        <Button onClick={onBackToList} variant="outline">
          ← Back to Conversations
        </Button>
        <h1 className={styles.title}>Conversation Details</h1>
      </div>

      <div className={styles.conversationMeta}>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Conversation ID:</span>
          <span className={styles.metaValue} title={conversationId}>
            {formatConversationId(conversationId)}
          </span>
        </div>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Total Messages:</span>
          <span className={styles.metaValue}>{totalMessages}</span>
        </div>
      </div>

      {messages.length === 0 ? (
        <div className={styles.emptyState}>
          <p>No messages found in this conversation.</p>
        </div>
      ) : (
        <>
          {/* Load more earlier messages button */}
          {hasMoreBefore && (
            <div className={styles.loadMoreContainer}>
              <Button
                onClick={loadMoreBefore}
                disabled={loadingMore}
                variant="outline"
                className={styles.loadMoreButton}
              >
                {loadingMore ? 'Loading...' : 'Load earlier messages'}
              </Button>
            </div>
          )}

          <div className={styles.messagesContainer}>
            {messageTurns.map((turn, turnIndex) => (
              <div key={turnIndex} className={styles.messageTurn}>
                <div className={styles.turnLabel}>Turn {turnIndex + 1}</div>
                <div className={styles.turnMessages}>
                  {turn.map((message) => (
                    <MessageDisplay key={message.internal_id} message={message} />
                  ))}
                </div>
              </div>
            ))}
          </div>

          {/* Load more recent messages button */}
          {hasMoreAfter && (
            <div className={styles.loadMoreContainer}>
              <Button
                onClick={loadMoreAfter}
                disabled={loadingMore}
                variant="outline"
                className={styles.loadMoreButton}
              >
                {loadingMore ? 'Loading...' : 'Load newer messages'}
              </Button>
            </div>
          )}

          {/* Floating jump to latest button - only show when user is not viewing the latest messages */}
          {hasMoreAfter && (
            <div className={styles.floatingButton}>
              <Button
                onClick={jumpToLatest}
                disabled={loading}
                variant="default"
                className={styles.jumpToLatestButton}
              >
                ↓ Jump to latest
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default ConversationView;
