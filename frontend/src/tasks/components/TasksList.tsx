import React, { useState, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import TasksFilter, { Filters } from './TasksFilter';
import TaskCard from './TaskCard';
import styles from './TasksList.module.css';

// Type Definitions
interface Task {
  id: number;
  task_id: string;
  status: 'pending' | 'processing' | 'done' | 'failed';
  task_type: string;
  created_at: string;
  scheduled_at: string | null;
  retry_count: number;
  max_retries: number;
  recurrence_rule: string | null;
  error_message: string | null;
  payload: Record<string, any> | null;
}

interface TasksResponse {
  tasks: Task[];
}

interface TasksListProps {
  onLoadingChange?: (loading: boolean) => void;
}

const TasksList: React.FC<TasksListProps> = ({ onLoadingChange }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskTypes, setTaskTypes] = useState<string[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const filters: Filters = {
    status: searchParams.get('status') || '',
    task_type: searchParams.get('task_type') || '',
    date_from: searchParams.get('date_from') || '',
    date_to: searchParams.get('date_to') || '',
    sort: (searchParams.get('sort') as 'asc' | 'desc' | null) || 'desc',
  };

  const hasActiveFilters =
    filters.status ||
    filters.task_type ||
    filters.date_from ||
    filters.date_to ||
    filters.sort !== 'desc';

  const fetchTasks = useCallback(async () => {
    try {
      setLoading(true);
      onLoadingChange?.(true);
      setError(null);

      const params = new URLSearchParams();
      if (filters.status) {
        params.set('status', filters.status);
      }
      if (filters.task_type) {
        params.set('task_type', filters.task_type);
      }
      if (filters.date_from) {
        params.set('date_from', filters.date_from);
      }
      if (filters.date_to) {
        params.set('date_to', filters.date_to);
      }
      if (filters.sort !== 'desc') {
        params.set('sort', filters.sort);
      }
      params.set('limit', '500');

      const response = await fetch(`/api/tasks/?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch tasks: ${response.status} ${response.statusText}`);
      }

      const data: TasksResponse = await response.json();
      setTasks(data.tasks || []);
    } catch (err: any) {
      console.error('Error fetching tasks:', err);
      setError(err.message);
    } finally {
      setLoading(false);
      onLoadingChange?.(false);
    }
  }, [searchParams, onLoadingChange]);

  const fetchTaskTypes = async () => {
    try {
      const response = await fetch('/api/tasks/?limit=1000');
      if (response.ok) {
        const data: TasksResponse = await response.json();
        const types = [...new Set((data.tasks || []).map((task) => task.task_type))].sort();
        setTaskTypes(types);
      }
    } catch (err) {
      console.error('Error fetching task types:', err);
    }
  };

  const handleFilterChange = (newFilters: Filters) => {
    const params = new URLSearchParams();
    (Object.keys(newFilters) as Array<keyof Filters>).forEach((key) => {
      if (newFilters[key] && (key !== 'sort' || newFilters[key] !== 'desc')) {
        params.set(key, newFilters[key].toString());
      }
    });
    setSearchParams(params);
  };

  const handleRetry = async (taskId: number) => {
    try {
      const response = await fetch(`/api/tasks/${taskId}/retry`, { method: 'POST' });
      if (!response.ok) {
        throw new Error(`Retry failed: ${response.status} ${response.statusText}`);
      }
      await fetchTasks();
    } catch (err: any) {
      console.error('Error retrying task:', err);
      window.alert(`Failed to retry task: ${err.message}`);
    }
  };

  const clearFilters = () => {
    setSearchParams({});
  };

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  useEffect(() => {
    fetchTaskTypes();
  }, []);

  if (loading) {
    return <div className={styles.loading}>Loading tasks...</div>;
  }

  if (error) {
    return (
      <div className={styles.errorContainer}>
        <div className={styles.errorMessage}>Error loading tasks: {error}</div>
        <Button onClick={fetchTasks} variant="secondary">
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div className={styles.tasksList}>
      <header className={styles.header}>
        <h1>Task Queue</h1>
        <p>View and manage background tasks. Shows up to 500 tasks based on current filters.</p>
      </header>

      <TasksFilter
        filters={filters}
        taskTypes={taskTypes}
        onFilterChange={handleFilterChange}
        hasActiveFilters={hasActiveFilters}
        onClearFilters={clearFilters}
      />

      {tasks.length === 0 ? (
        <div className={styles.emptyState}>
          {hasActiveFilters ? 'No tasks match the current filters.' : 'No tasks found.'}
        </div>
      ) : (
        <div className={styles.resultsContainer}>
          <div className={styles.resultsSummary}>
            Showing {tasks.length} task{tasks.length !== 1 ? 's' : ''}
            {hasActiveFilters && (
              <span>
                {' '}
                (filtered)
                <Button onClick={clearFilters} variant="ghost" size="sm">
                  Clear filters
                </Button>
              </span>
            )}
          </div>

          <div className={styles.tasksGrid}>
            {tasks.map((task) => (
              <TaskCard key={task.id} task={task} onRetry={handleRetry} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default TasksList;