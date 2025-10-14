import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import MessageDisplay from './MessageDisplay';
import styles from './ConversationView.module.css';

interface Message {
  internal_id: string;
  timestamp: string;
  role: 'user' | 'assistant' | 'tool' | 'system';
  tool_calls?: any[];
  content?: string;
  [key: string]: any;
}

interface MessagesResponse {
  messages: Message[];
  total_messages: number;
  has_more_before: boolean;
  has_more_after: boolean;
}

interface ConversationViewProps {
  onBackToList: () => void;
}

const ConversationView: React.FC<ConversationViewProps> = ({ onBackToList }) => {
  const { conversationId } = useParams<{ conversationId: string }>();
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [totalMessages, setTotalMessages] = useState<number>(0);
  const [hasMoreBefore, setHasMoreBefore] = useState<boolean>(false);
  const [hasMoreAfter, setHasMoreAfter] = useState<boolean>(false);
  const [loadingMore, setLoadingMore] = useState<boolean>(false);
  const hasInitiallyLoadedRef = useRef<boolean>(false);

  const fetchMessages = useCallback(
    async (before: string | null = null, after: string | null = null, append = false) => {
      if (!append) {
        setLoading(true);
        setError(null);
      } else {
        setLoadingMore(true);
      }

      try {
        const params = new URLSearchParams();
        params.set('limit', '50');

        if (before) {
          params.set('before', before);
        }
        if (after) {
          params.set('after', after);
        }

        const response = await fetch(
          `/api/v1/chat/conversations/${encodeURIComponent(conversationId!)}/messages?${params}`
        );

        if (!response.ok) {
          throw new Error(`Failed to fetch messages: ${response.statusText}`);
        }

        const data: MessagesResponse = await response.json();
        const newMessages = data.messages || [];

        if (append) {
          if (before) {
            setMessages((prev) => [...newMessages, ...prev]);
          } else if (after) {
            setMessages((prev) => [...prev, ...newMessages]);
          }
        } else {
          setMessages(newMessages);
        }

        setTotalMessages(data.total_messages || 0);
        setHasMoreBefore(data.has_more_before || false);
        setHasMoreAfter(data.has_more_after || false);
      } catch (err: any) {
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

  useEffect(() => {
    if (!loading && messages.length > 0 && !hasInitiallyLoadedRef.current) {
      hasInitiallyLoadedRef.current = true;
      setTimeout(() => {
        window.scrollTo(0, document.body.scrollHeight);
      }, 100);
    }
  }, [loading, messages.length]);

  const loadMoreBefore = useCallback(() => {
    if (messages.length === 0 || loadingMore) {
      return;
    }
    const oldestMessage = messages[0];
    fetchMessages(oldestMessage.timestamp, null, true);
  }, [messages, loadingMore, fetchMessages]);

  const loadMoreAfter = useCallback(() => {
    if (messages.length === 0 || loadingMore) {
      return;
    }
    const newestMessage = messages[messages.length - 1];
    fetchMessages(null, newestMessage.timestamp, true);
  }, [messages, loadingMore, fetchMessages]);

  const jumpToLatest = useCallback(() => {
    fetchMessages();
  }, [fetchMessages]);

  const groupMessagesByTurn = (messages: Message[]): Message[][] => {
    const turns: Message[][] = [];
    let currentTurn: Message[] = [];

    for (const message of messages) {
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

  const formatConversationId = (id: string) => {
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
            {formatConversationId(conversationId!)}
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
