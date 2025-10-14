import React from 'react';
import { Button } from '@/components/ui/button';
import JsonPayloadViewer from './JsonPayloadViewer';
import styles from './TaskCard.module.css';

// Type Definitions
type TaskStatus = 'pending' | 'processing' | 'done' | 'failed';

interface Task {
  id: number;
  task_id: string;
  status: TaskStatus;
  task_type: string;
  created_at: string;
  scheduled_at: string | null;
  retry_count: number;
  max_retries: number;
  recurrence_rule: string | null;
  error_message: string | null;
  payload: Record<string, any> | null;
}

interface TaskCardProps {
  task: Task;
  onRetry: (taskId: number) => void;
}

const TaskCard: React.FC<TaskCardProps> = ({ task, onRetry }) => {
  const formatDate = (dateString: string | null): string => {
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

  const getStatusClass = (status: TaskStatus): string => {
    switch (status?.toLowerCase()) {
      case 'pending':
        return `${styles.statusBadge} ${styles.statusPending}`;
      case 'processing':
        return `${styles.statusBadge} ${styles.statusProcessing}`;
      case 'done':
        return `${styles.statusBadge} ${styles.statusDone}`;
      case 'failed':
        return `${styles.statusBadge} ${styles.statusFailed}`;
      default:
        return `${styles.statusBadge} ${styles.statusDefault}`;
    }
  };

  const canRetry = (status: TaskStatus): boolean => {
    return ['failed', 'processing'].includes(status?.toLowerCase());
  };

  const handleRetryClick = () => {
    if (window.confirm('Are you sure you want to retry this task?')) {
      onRetry(task.id);
    }
  };

  return (
    <div className={styles.taskCard}>
      <div className={styles.taskHeader}>
        <div>
          <h4 className={styles.taskTitle}>Task #{task.id}</h4>
          <div className={styles.taskId}>Task ID: {task.task_id}</div>
        </div>
        <div className={styles.taskActions}>
          <span className={getStatusClass(task.status)}>{task.status}</span>
          {canRetry(task.status) && (
            <Button onClick={handleRetryClick} size="sm" title="Manually retry this task">
              Retry
            </Button>
          )}
        </div>
      </div>

      <div className={styles.taskDetailsGrid}>
        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Type:</div>
          <div className={`${styles.detailValue} ${styles.detailMonospace}`}>{task.task_type}</div>
        </div>

        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Created:</div>
          <div className={styles.detailValue}>{formatDate(task.created_at)}</div>
        </div>

        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Scheduled:</div>
          <div className={styles.detailValue}>{formatDate(task.scheduled_at)}</div>
        </div>

        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Retries:</div>
          <div className={styles.detailValue}>
            {task.retry_count} / {task.max_retries}
          </div>
        </div>
      </div>

      {task.recurrence_rule && (
        <div className={styles.recurrenceSection}>
          <div className={styles.detailLabel}>Recurrence Rule:</div>
          <div className={styles.recurrenceValue}>{task.recurrence_rule}</div>
        </div>
      )}

      {task.status?.toLowerCase() === 'failed' && task.error_message && (
        <div className={styles.errorSection}>
          <div className={styles.errorLabel}>Error Message:</div>
          <div className={styles.errorMessage}>{task.error_message}</div>
        </div>
      )}

      {task.payload && (
        <div className={styles.payloadSection}>
          <div className={styles.detailLabel}>Payload:</div>
          <div className={styles.payloadValue}>
            <JsonPayloadViewer data={task.payload} taskId={task.id.toString()} />
          </div>
        </div>
      )}

      {!task.payload && <div className={styles.noPayload}>No payload data</div>}
    </div>
  );
};

export default TaskCard;
