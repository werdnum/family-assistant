import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import styles from './EventDetail.module.css';

const EventDetail = ({ onBackToList }) => {
  const { eventId } = useParams();
  const [event, setEvent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchEvent = async () => {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(`/api/events/${encodeURIComponent(eventId)}`);

        if (!response.ok) {
          if (response.status === 404) {
            throw new Error('Event not found');
          }
          throw new Error(`Failed to fetch event: ${response.statusText}`);
        }

        const eventData = await response.json();
        setEvent(eventData);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (eventId) {
      fetchEvent();
    }
  }, [eventId]);

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown';
    }
    return new Date(timestamp).toLocaleString();
  };

  const getSourceIcon = (sourceId) => {
    switch (sourceId) {
      case 'homeassistant':
        return '🏠';
      case 'indexing':
        return '📚';
      case 'webhook':
        return '🔗';
      default:
        return '📋';
    }
  };

  const getSourceLabel = (sourceId) => {
    switch (sourceId) {
      case 'homeassistant':
        return 'Home Assistant';
      case 'indexing':
        return 'Indexing';
      case 'webhook':
        return 'Webhook';
      default:
        return sourceId || 'Unknown';
    }
  };

  const formatJson = (obj) => {
    return JSON.stringify(obj, null, 2);
  };

  if (loading) {
    return (
      <div className={styles.eventDetail}>
        <div className={styles.loading}>Loading event details...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.eventDetail}>
        <div className={styles.header}>
          <button onClick={onBackToList} className={styles.backButton}>
            ← Back to Events
          </button>
        </div>
        <div className={styles.error}>Error: {error}</div>
      </div>
    );
  }

  if (!event) {
    return (
      <div className={styles.eventDetail}>
        <div className={styles.header}>
          <button onClick={onBackToList} className={styles.backButton}>
            ← Back to Events
          </button>
        </div>
        <div className={styles.error}>Event not found</div>
      </div>
    );
  }

  return (
    <div className={styles.eventDetail}>
      <div className={styles.header}>
        <button onClick={onBackToList} className={styles.backButton}>
          ← Back to Events
        </button>
        <h1>Event Details</h1>
      </div>

      <div className={styles.eventCard}>
        <div className={styles.eventHeader}>
          <div className={styles.eventId}>
            <h2>{event.event_id}</h2>
          </div>
          <div className={styles.sourceBadge}>
            <span
              className={styles.sourceIcon}
              title={`Source: ${getSourceLabel(event.source_id)}`}
            >
              {getSourceIcon(event.source_id)}
            </span>
            <span className={styles.sourceLabel}>{getSourceLabel(event.source_id)}</span>
          </div>
        </div>

        <div className={styles.eventInfo}>
          <div className={styles.infoGrid}>
            <div className={styles.infoItem}>
              <label className={styles.infoLabel}>Event ID:</label>
              <div className={styles.infoValue}>
                <code className={styles.eventIdCode}>{event.event_id}</code>
              </div>
            </div>

            <div className={styles.infoItem}>
              <label className={styles.infoLabel}>Source:</label>
              <div className={styles.infoValue}>{getSourceLabel(event.source_id)}</div>
            </div>

            <div className={styles.infoItem}>
              <label className={styles.infoLabel}>Timestamp:</label>
              <div className={styles.infoValue}>{formatTimestamp(event.timestamp)}</div>
            </div>

            {event.triggered_listener_ids && event.triggered_listener_ids.length > 0 && (
              <div className={styles.infoItem}>
                <label className={styles.infoLabel}>Triggered Listeners:</label>
                <div className={styles.infoValue}>
                  {event.triggered_listener_ids.length} listener
                  {event.triggered_listener_ids.length !== 1 ? 's' : ''}
                  <div className={styles.listenerIds}>
                    {event.triggered_listener_ids.map((listenerId) => (
                      <span key={listenerId} className={styles.listenerId}>
                        #{listenerId}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {event.event_data && (
          <div className={styles.eventDataSection}>
            <h3 className={styles.sectionTitle}>Event Data</h3>
            <div className={styles.eventDataContainer}>
              <pre className={styles.eventDataJson}>
                <code>{formatJson(event.event_data)}</code>
              </pre>
            </div>
          </div>
        )}

        {(!event.triggered_listener_ids || event.triggered_listener_ids.length === 0) && (
          <div className={styles.noListenersSection}>
            <h3 className={styles.sectionTitle}>Triggered Listeners</h3>
            <p className={styles.noListenersText}>No listeners were triggered by this event.</p>
          </div>
        )}
      </div>
    </div>
  );
};

export default EventDetail;
