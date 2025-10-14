import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import styles from './EventCard.module.css';

interface EventData {
  event_type?: string;
  type?: string;
  data?: {
    entity_id?: string;
  };
  document_type?: string;
  document_id?: string;
  status?: string;
  method?: string;
  path?: string;
  [key: string]: any;
}

interface Event {
  event_id: string;
  source_id: string;
  timestamp: string;
  event_data: EventData | null;
  triggered_listener_ids?: string[];
}

interface EventCardProps {
  event: Event;
}

const EventCard: React.FC<EventCardProps> = ({ event }) => {
  const [isExpanded, setIsExpanded] = useState(false);

  const formatTimestamp = (timestamp: string): string => {
    if (!timestamp) {
      return 'Unknown';
    }
    return new Date(timestamp).toLocaleString();
  };

  const getSourceIcon = (sourceId: string): string => {
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

  const getSourceLabel = (sourceId: string): string => {
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

  const getEventSummary = (event: Event): string => {
    if (!event.event_data) {
      return 'No event data available';
    }

    const data = event.event_data;

    if (event.source_id === 'homeassistant') {
      if (data.event_type) {
        const entityId = data.data?.entity_id || 'unknown entity';
        return `${data.event_type}: ${entityId}`;
      }
      return 'Home Assistant event';
    }

    if (event.source_id === 'indexing') {
      if (data.document_type && data.document_id) {
        return `Indexed ${data.document_type}: ${data.document_id}`;
      }
      if (data.status) {
        return `Indexing ${data.status}`;
      }
      return 'Document indexing event';
    }

    if (event.source_id === 'webhook') {
      if (data.method && data.path) {
        return `${data.method} ${data.path}`;
      }
      return 'Webhook event';
    }

    if (data.event_type || data.type) {
      return data.event_type || data.type;
    }

    return 'Event occurred';
  };

  const truncateEventId = (id: string): string => {
    if (id.length > 30) {
      return `${id.substring(0, 15)}...${id.substring(id.length - 15)}`;
    }
    return id;
  };

  const formatJson = (obj: any): string => {
    return JSON.stringify(obj, null, 2);
  };

  return (
    <div className={styles.eventCard}>
      <div className={styles.eventHeader}>
        <div className={styles.eventTitle}>
          <h3>
            <Link to={`/events/${encodeURIComponent(event.event_id)}`} className={styles.eventLink}>
              {truncateEventId(event.event_id)}
            </Link>
          </h3>
        </div>
        <div className={styles.sourceBadge}>
          <span className={styles.sourceIcon} title={`Source: ${getSourceLabel(event.source_id)}`}>
            {getSourceIcon(event.source_id)}
          </span>
          <span className={styles.sourceLabel}>{getSourceLabel(event.source_id)}</span>
        </div>
      </div>

      <div className={styles.eventSummary}>
        <p className={styles.summaryText}>{getEventSummary(event)}</p>
      </div>

      {event.event_data && (
        <div className={styles.eventDataSection}>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setIsExpanded(!isExpanded)}
            aria-expanded={isExpanded}
          >
            {isExpanded ? '▼' : '▶'} Event Data
          </Button>

          {isExpanded && (
            <div className={styles.eventDataContainer}>
              <pre className={styles.eventDataJson}>
                <code>{formatJson(event.event_data)}</code>
              </pre>
            </div>
          )}
        </div>
      )}

      <div className={styles.eventMeta}>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Timestamp:</span>
          <span className={styles.metaValue}>{formatTimestamp(event.timestamp)}</span>
        </div>

        {event.triggered_listener_ids && event.triggered_listener_ids.length > 0 && (
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Triggered Listeners:</span>
            <span className={styles.metaValue}>
              {event.triggered_listener_ids.length} listener
              {event.triggered_listener_ids.length !== 1 ? 's' : ''}
            </span>
          </div>
        )}
      </div>

      <div className={styles.eventFooter}>
        <Link to={`/events/${encodeURIComponent(event.event_id)}`} className={styles.viewLink}>
          View Details →
        </Link>
      </div>
    </div>
  );
};

export default EventCard;
