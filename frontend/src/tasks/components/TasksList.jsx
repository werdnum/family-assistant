import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import TasksFilter from './TasksFilter';
import TaskCard from './TaskCard';
import styles from './TasksList.module.css';

const TasksList = ({ onLoadingChange }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState([]);
  const [taskTypes, setTaskTypes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Parse URL parameters for filters
  const filters = {
    status: searchParams.get('status') || '',
    task_type: searchParams.get('task_type') || '',
    date_from: searchParams.get('date_from') || '',
    date_to: searchParams.get('date_to') || '',
    sort: searchParams.get('sort') || 'desc',
  };

  const hasActiveFilters =
    filters.status ||
    filters.task_type ||
    filters.date_from ||
    filters.date_to ||
    filters.sort !== 'desc';

  // Fetch tasks from API
  const fetchTasks = async () => {
    try {
      setLoading(true);
      setError(null);

      // Build query parameters
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
      params.set('limit', '500'); // Match the Jinja2 implementation

      const response = await fetch(`/api/tasks/?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch tasks: ${response.status} ${response.statusText}`);
      }

      const data = await response.json();
      setTasks(data.tasks || []);
    } catch (err) {
      console.error('Error fetching tasks:', err);
      setError(err.message);
    } finally {
      setLoading(false);
      // Notify parent that loading is complete
      onLoadingChange?.(false);
    }
  };

  // Fetch task types for autocomplete
  const fetchTaskTypes = async () => {
    try {
      // Get all tasks to extract unique task types
      const response = await fetch('/api/tasks/?limit=1000');
      if (response.ok) {
        const data = await response.json();
        const types = [...new Set((data.tasks || []).map((task) => task.task_type))].sort();
        setTaskTypes(types);
      }
    } catch (err) {
      console.error('Error fetching task types:', err);
    }
  };

  // Handle filter changes
  const handleFilterChange = (newFilters) => {
    const params = new URLSearchParams();

    // Only add non-empty filters to URL
    Object.entries(newFilters).forEach(([key, value]) => {
      if (value && value !== 'desc' && key !== 'sort') {
        params.set(key, value);
      } else if (key === 'sort' && value !== 'desc') {
        params.set(key, value);
      }
    });

    setSearchParams(params);
  };

  // Handle task retry
  const handleRetry = async (taskId) => {
    try {
      const response = await fetch(`/api/tasks/${taskId}/retry`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error(`Retry failed: ${response.status} ${response.statusText}`);
      }

      // Refresh tasks list after successful retry
      await fetchTasks();
    } catch (err) {
      console.error('Error retrying task:', err);
      // eslint-disable-next-line no-alert
      window.alert(`Failed to retry task: ${err.message}`);
    }
  };

  // Clear all filters
  const clearFilters = () => {
    setSearchParams({});
  };

  // Fetch data on component mount and when filters change
  useEffect(() => {
    fetchTasks();
  }, [searchParams]);

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
