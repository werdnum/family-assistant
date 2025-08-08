import React, { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import MessageDisplay from './MessageDisplay';
import styles from './ConversationView.module.css';

const ConversationView = ({ onBackToList }) => {
  const { conversationId } = useParams();
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [totalMessages, setTotalMessages] = useState(0);

  // Pagination removed - all messages shown at once

  const fetchMessages = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch(
        `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages`
      );

      if (!response.ok) {
        throw new Error(`Failed to fetch messages: ${response.statusText}`);
      }

      const data = await response.json();
      setMessages(data.messages || []);
      setTotalMessages(data.total || 0);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    if (conversationId) {
      fetchMessages();
    }
  }, [conversationId, fetchMessages]);

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
          <button onClick={onBackToList} className={styles.backButton}>
            ← Back to Conversations
          </button>
        </div>
        <div className={styles.error}>Error: {error}</div>
      </div>
    );
  }

  const messageTurns = groupMessagesByTurn(messages);

  return (
    <div className={styles.conversationView}>
      <div className={styles.header}>
        <button onClick={onBackToList} className={styles.backButton}>
          ← Back to Conversations
        </button>
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

          {/* Note: Message pagination not implemented - all messages are shown */}
        </>
      )}
    </div>
  );
};

export default ConversationView;
