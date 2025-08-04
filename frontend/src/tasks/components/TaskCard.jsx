import React from 'react';
import JsonPayloadViewer from './JsonPayloadViewer';

const TaskCard = ({ task, onRetry }) => {
  // Format dates for display
  const formatDate = (dateString) => {
    if (!dateString) {
      return 'N/A';
    }
    try {
      const date = new Date(dateString);
      return date.toLocaleString();
    } catch {
      return 'Invalid Date';
    }
  };

  // Get status color and styles
  const getStatusStyle = (status) => {
    const baseStyle = {
      padding: '0.25rem 0.5rem',
      borderRadius: '3px',
      fontSize: '0.85rem',
      fontWeight: 'bold',
      textTransform: 'uppercase',
    };

    switch (status?.toLowerCase()) {
      case 'pending':
        return { ...baseStyle, backgroundColor: '#ffc107', color: '#000' };
      case 'processing':
        return { ...baseStyle, backgroundColor: '#17a2b8', color: '#fff' };
      case 'done':
        return { ...baseStyle, backgroundColor: '#28a745', color: '#fff' };
      case 'failed':
        return { ...baseStyle, backgroundColor: '#dc3545', color: '#fff' };
      default:
        return { ...baseStyle, backgroundColor: '#6c757d', color: '#fff' };
    }
  };

  // Check if task can be retried
  const canRetry = (status) => {
    return ['failed', 'processing'].includes(status?.toLowerCase());
  };

  const handleRetryClick = () => {
    // eslint-disable-next-line no-alert
    if (window.confirm('Are you sure you want to retry this task?')) {
      onRetry(task.id);
    }
  };

  return (
    <div
      style={{
        border: '1px solid #ddd',
        borderRadius: '5px',
        padding: '1rem',
        backgroundColor: '#fff',
        marginBottom: '1rem',
      }}
    >
      {/* Header with ID and Status */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.75rem',
          borderBottom: '1px solid #eee',
          paddingBottom: '0.5rem',
        }}
      >
        <div>
          <h4 style={{ margin: 0, fontSize: '1.1rem' }}>Task #{task.id}</h4>
          <div style={{ fontSize: '0.9rem', color: '#666', marginTop: '0.25rem' }}>
            Task ID: {task.task_id}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={getStatusStyle(task.status)}>{task.status}</span>
          {canRetry(task.status) && (
            <button
              onClick={handleRetryClick}
              style={{
                padding: '0.25rem 0.5rem',
                fontSize: '0.8rem',
                backgroundColor: '#007bff',
                color: 'white',
                border: 'none',
                borderRadius: '3px',
                cursor: 'pointer',
              }}
              title="Manually retry this task"
            >
              Retry
            </button>
          )}
        </div>
      </div>

      {/* Task Details Grid */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: '1rem',
          marginBottom: '1rem',
        }}
      >
        {/* Task Type */}
        <div>
          <strong>Type:</strong>
          <div style={{ marginTop: '0.25rem', fontFamily: 'monospace', fontSize: '0.9rem' }}>
            {task.task_type}
          </div>
        </div>

        {/* Created At */}
        <div>
          <strong>Created:</strong>
          <div style={{ marginTop: '0.25rem', fontSize: '0.9rem' }}>
            {formatDate(task.created_at)}
          </div>
        </div>

        {/* Scheduled At */}
        <div>
          <strong>Scheduled:</strong>
          <div style={{ marginTop: '0.25rem', fontSize: '0.9rem' }}>
            {formatDate(task.scheduled_at)}
          </div>
        </div>

        {/* Retry Count */}
        <div>
          <strong>Retries:</strong>
          <div style={{ marginTop: '0.25rem', fontSize: '0.9rem' }}>
            {task.retry_count} / {task.max_retries}
          </div>
        </div>
      </div>

      {/* Recurrence Rule (if available) */}
      {task.recurrence_rule && (
        <div style={{ marginBottom: '1rem' }}>
          <strong>Recurrence Rule:</strong>
          <div
            style={{
              marginTop: '0.25rem',
              fontFamily: 'monospace',
              fontSize: '0.9rem',
              backgroundColor: '#f8f9fa',
              padding: '0.5rem',
              borderRadius: '3px',
            }}
          >
            {task.recurrence_rule}
          </div>
        </div>
      )}

      {/* Error Message (if failed) */}
      {task.status?.toLowerCase() === 'failed' && task.error_message && (
        <div style={{ marginBottom: '1rem' }}>
          <strong style={{ color: '#dc3545' }}>Error Message:</strong>
          <div
            style={{
              marginTop: '0.25rem',
              color: '#dc3545',
              backgroundColor: '#f8d7da',
              border: '1px solid #f5c6cb',
              borderRadius: '3px',
              padding: '0.5rem',
              fontSize: '0.9rem',
              fontFamily: 'monospace',
            }}
          >
            {task.error_message}
          </div>
        </div>
      )}

      {/* Payload */}
      {task.payload && (
        <div>
          <strong>Payload:</strong>
          <div style={{ marginTop: '0.5rem' }}>
            <JsonPayloadViewer data={task.payload} taskId={task.id} />
          </div>
        </div>
      )}

      {/* Show empty payload message if no payload */}
      {!task.payload && (
        <div
          style={{
            padding: '0.5rem',
            backgroundColor: '#f8f9fa',
            border: '1px solid #e9ecef',
            borderRadius: '3px',
            color: '#6c757d',
            fontSize: '0.9rem',
            fontStyle: 'italic',
          }}
        >
          No payload data
        </div>
      )}
    </div>
  );
};

export default TaskCard;
