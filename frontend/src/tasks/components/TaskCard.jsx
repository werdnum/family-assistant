import React, { useState } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import JsonPayloadViewer from './JsonPayloadViewer';
import styles from './TaskCard.module.css';

const TaskCard = ({ task, onRetry, onCancel }) => {
  const [showRetryDialog, setShowRetryDialog] = useState(false);
  const [showCancelDialog, setShowCancelDialog] = useState(false);
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

  // Get status CSS class
  const getStatusClass = (status) => {
    switch (status?.toLowerCase()) {
      case 'pending':
        return `${styles.statusBadge} ${styles.statusPending}`;
      case 'processing':
        return `${styles.statusBadge} ${styles.statusProcessing}`;
      case 'done':
        return `${styles.statusBadge} ${styles.statusDone}`;
      case 'failed':
        return `${styles.statusBadge} ${styles.statusFailed}`;
      case 'cancelled':
        return `${styles.statusBadge} ${styles.statusCancelled}`;
      default:
        return `${styles.statusBadge} ${styles.statusDefault}`;
    }
  };

  // Check if task can be retried
  const canRetry = (status) => {
    return ['failed', 'processing'].includes(status?.toLowerCase());
  };

  // Check if task can be cancelled
  const canCancel = (status) => {
    return status?.toLowerCase() === 'pending';
  };

  const handleRetryClick = () => {
    setShowRetryDialog(true);
  };

  const handleRetryConfirm = () => {
    setShowRetryDialog(false);
    onRetry(task.id);
  };

  const handleCancelClick = () => {
    setShowCancelDialog(true);
  };

  const handleCancelConfirm = () => {
    setShowCancelDialog(false);
    onCancel(task.id);
  };

  return (
    <div className={styles.taskCard}>
      {/* Header with ID and Status */}
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
          {canCancel(task.status) && (
            <Button
              onClick={handleCancelClick}
              size="sm"
              variant="destructive"
              title="Cancel this task"
            >
              Cancel
            </Button>
          )}
        </div>
      </div>

      {/* Task Details Grid */}
      <div className={styles.taskDetailsGrid}>
        {/* Task Type */}
        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Type:</div>
          <div className={`${styles.detailValue} ${styles.detailMonospace}`}>{task.task_type}</div>
        </div>

        {/* Created At */}
        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Created:</div>
          <div className={styles.detailValue}>{formatDate(task.created_at)}</div>
        </div>

        {/* Scheduled At */}
        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Scheduled:</div>
          <div className={styles.detailValue}>{formatDate(task.scheduled_at)}</div>
        </div>

        {/* Retry Count */}
        <div className={styles.detailItem}>
          <div className={styles.detailLabel}>Retries:</div>
          <div className={styles.detailValue}>
            {task.retry_count} / {task.max_retries}
          </div>
        </div>
      </div>

      {/* Recurrence Rule (if available) */}
      {task.recurrence_rule && (
        <div className={styles.recurrenceSection}>
          <div className={styles.detailLabel}>Recurrence Rule:</div>
          <div className={styles.recurrenceValue}>{task.recurrence_rule}</div>
        </div>
      )}

      {/* Error Message (if failed) */}
      {task.status?.toLowerCase() === 'failed' && task.error_message && (
        <div className={styles.errorSection}>
          <div className={styles.errorLabel}>Error Message:</div>
          <div className={styles.errorMessage}>{task.error_message}</div>
        </div>
      )}

      {/* Payload */}
      {task.payload && (
        <div className={styles.payloadSection}>
          <div className={styles.detailLabel}>Payload:</div>
          <div className={styles.payloadValue}>
            <JsonPayloadViewer data={task.payload} taskId={task.id} />
          </div>
        </div>
      )}

      {/* Show empty payload message if no payload */}
      {!task.payload && <div className={styles.noPayload}>No payload data</div>}

      {/* Retry Confirmation Dialog */}
      <AlertDialog open={showRetryDialog} onOpenChange={setShowRetryDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Retry Task</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to retry this task? This will reschedule it for immediate
              execution.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleRetryConfirm}>Retry</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Cancel Confirmation Dialog */}
      <AlertDialog open={showCancelDialog} onOpenChange={setShowCancelDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cancel Task</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to cancel this task? This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>No, Keep Task</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleCancelConfirm}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Yes, Cancel Task
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};

export default TaskCard;
