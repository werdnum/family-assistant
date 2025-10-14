import React, { useState, useEffect } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';

interface ActionConfig {
  script_code?: string;
  timeout?: number;
  llm_callback_prompt?: string;
}

interface EventListener {
  id: string;
  name: string;
  description: string | null;
  source_id: string;
  action_type: 'wake_llm' | 'script';
  enabled: boolean;
  one_time: boolean;
  created_at: string;
  conversation_id: string;
  interface_type: string;
  match_conditions: Record<string, unknown>;
  condition_script: string | null;
  action_config?: ActionConfig;
  daily_executions?: number;
  last_execution_at?: string;
}

interface Stats {
  total_executions: number;
  daily_executions: number;
  daily_limit: number;
  last_execution_at: string | null;
  recent_events: any[];
}

interface TaskExecution {
  id: string;
  created_at: string;
  status: 'done' | 'failed' | 'processing' | 'pending';
  error: string | null;
  retry_count: number;
  max_retries: number;
}

const EventListenerDetail: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [listener, setListener] = useState<EventListener | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [taskExecutions, setTaskExecutions] = useState<TaskExecution[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<boolean>(false);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const listenerResponse = await fetch(`/api/event-listeners/${id}`);
        if (!listenerResponse.ok) {
          throw new Error(`Failed to fetch listener: ${listenerResponse.statusText}`);
        }
        const listenerData: EventListener = await listenerResponse.json();
        setListener(listenerData);

        setStats({
          total_executions: listenerData.daily_executions || 0,
          daily_executions: listenerData.daily_executions || 0,
          daily_limit: 5,
          last_execution_at: listenerData.last_execution_at || null,
          recent_events: [],
        });
        setTaskExecutions([]);
      } catch (err: any) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (id) {
      fetchData();
    }
  }, [id]);

  const handleToggleEnabled = async () => {
    if (!listener) {
      return;
    }

    setUpdating(true);
    try {
      const url = new window.URL(`/api/event-listeners/${id}`, window.location.origin);
      url.searchParams.set('conversation_id', listener.conversation_id || 'web');

      const response = await fetch(url.toString(), {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          enabled: !listener.enabled,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to update listener: ${response.statusText}`);
      }

      const updatedListener: EventListener = await response.json();
      setListener(updatedListener);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUpdating(false);
    }
  };

  const handleDelete = async () => {
    if (!listener) {
      return;
    }

    const confirmed = window.confirm(
      'Are you sure you want to delete this listener? This action cannot be undone.'
    );

    if (!confirmed) {
      return;
    }

    try {
      const url = new window.URL(`/api/event-listeners/${id}`, window.location.origin);
      url.searchParams.set('conversation_id', listener.conversation_id || 'web');

      const response = await fetch(url.toString(), {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error(`Failed to delete listener: ${response.statusText}`);
      }

      navigate('/event-listeners');
    } catch (err: any) {
      setError(err.message);
    }
  };

  const formatTimestamp = (timestamp: string | null | undefined): string => {
    if (!timestamp) {
      return 'Never';
    }
    return new Date(timestamp).toLocaleString();
  };

  const getActionIcon = (actionType: 'wake_llm' | 'script'): string => {
    return actionType === 'wake_llm' ? '🤖' : '📜';
  };

  const getActionTitle = (actionType: 'wake_llm' | 'script'): string => {
    return actionType === 'wake_llm' ? 'LLM Callback' : 'Script Execution';
  };

  const formatSourceId = (sourceId: string): string => {
    return sourceId.replace('_', ' ').replace(/\b\w/g, (l) => l.toUpperCase());
  };

  const formatMatchConditions = (conditions: Record<string, unknown> | null): string[] => {
    if (!conditions || typeof conditions !== 'object') {
      return [];
    }

    const formatted: string[] = [];
    for (const [key, value] of Object.entries(conditions)) {
      if (typeof value === 'object' && value !== null) {
        for (const [subKey, subValue] of Object.entries(value)) {
          formatted.push(`${key}.${subKey} = ${JSON.stringify(subValue)}`);
        }
      } else {
        formatted.push(`${key} = ${JSON.stringify(value)}`);
      }
    }
    return formatted;
  };

  if (loading) {
    return <div>Loading event listener...</div>;
  }
  if (error) {
    return <div className="error">Error: {error}</div>;
  }
  if (!listener) {
    return <div>Listener not found</div>;
  }

  const formattedConditions = formatMatchConditions(listener.match_conditions);

  return (
    <div className="event-listener-detail">
      <h1>
        {getActionIcon(listener.action_type)} {listener.name}
      </h1>

      <nav style={{ marginBottom: '2rem' }}>
        <Link to="/event-listeners">← Back to Event Listeners</Link>
      </nav>

      {error && (
        <div className="error" style={{ marginBottom: '1rem' }}>
          Error: {error}
        </div>
      )}

      <section>
        <h2>Configuration</h2>
        <dl>
          <dt>ID:</dt>
          <dd>{listener.id}</dd>

          <dt>Description:</dt>
          <dd>{listener.description || 'No description provided'}</dd>

          <dt>Source Type:</dt>
          <dd>{formatSourceId(listener.source_id)}</dd>

          <dt>Action Type:</dt>
          <dd>{getActionTitle(listener.action_type)}</dd>

          <dt>Status:</dt>
          <dd>
            {listener.enabled ? (
              <span style={{ color: 'var(--accent)' }}>✓ Enabled</span>
            ) : (
              <span style={{ color: 'var(--text-light)' }}>✗ Disabled</span>
            )}
            {listener.one_time && ' (One-time listener)'}
          </dd>

          <dt>Created:</dt>
          <dd>{formatTimestamp(listener.created_at)}</dd>

          <dt>Conversation ID:</dt>
          <dd>
            <code>{listener.conversation_id}</code>
          </dd>

          <dt>Interface Type:</dt>
          <dd>{listener.interface_type}</dd>
        </dl>
      </section>

      <section style={{ marginTop: '2rem' }}>
        <h2>Trigger Conditions</h2>
        <p style={{ color: 'var(--text-light)' }}>
          Events must match these conditions to trigger this listener.
        </p>

        {listener.condition_script ? (
          <>
            <h3>Condition Script (Starlark)</h3>
            <div
              style={{
                backgroundColor: 'var(--bg-secondary)',
                padding: '1rem',
                borderRadius: '8px',
                overflowX: 'auto',
              }}
            >
              <pre>
                <code>{listener.condition_script}</code>
              </pre>
            </div>
            <p style={{ color: 'var(--text-light)', fontSize: '0.9em', marginTop: '0.5rem' }}>
              This script receives an 'event' variable and must return True/False to determine if
              the listener triggers.
            </p>
          </>
        ) : formattedConditions.length > 0 ? (
          <>
            <h3>JSON Match Conditions</h3>
            <ul>
              {formattedConditions.map((condition, index) => (
                <li key={index}>
                  <code>{condition}</code>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p>
            <em>No conditions defined (matches all events from source).</em>
          </p>
        )}
      </section>

      <section style={{ marginTop: '2rem' }}>
        {listener.action_type === 'script' ? (
          <>
            <h2>Script Code</h2>
            {listener.action_config?.script_code ? (
              <>
                <div
                  style={{
                    backgroundColor: 'var(--bg-secondary)',
                    padding: '1rem',
                    borderRadius: '8px',
                    overflowX: 'auto',
                  }}
                >
                  <pre>
                    <code>{listener.action_config.script_code}</code>
                  </pre>
                </div>
                <p style={{ marginTop: '1rem' }}>
                  <strong>Timeout:</strong> {listener.action_config.timeout || 600} seconds
                </p>
              </>
            ) : (
              <p>
                <em>No script code defined.</em>
              </p>
            )}
          </>
        ) : (
          <>
            <h2>LLM Callback Configuration</h2>
            <dl>
              <dt>Callback Prompt:</dt>
              <dd>
                {listener.action_config?.llm_callback_prompt ? (
                  <pre
                    style={{
                      backgroundColor: 'var(--bg-secondary)',
                      padding: '1rem',
                      borderRadius: '8px',
                    }}
                  >
                    {listener.action_config.llm_callback_prompt}
                  </pre>
                ) : (
                  <em>Default prompt will be used</em>
                )}
              </dd>
            </dl>
          </>
        )}
      </section>

      <section style={{ marginTop: '2rem' }}>
        <h2>Execution Statistics</h2>
        <dl>
          <dt>Total Executions:</dt>
          <dd>{stats?.total_executions || 0}</dd>

          <dt>Today's Executions:</dt>
          <dd>
            {stats?.daily_executions || 0} / {stats?.daily_limit || 5}
            {(stats?.daily_executions || 0) >= (stats?.daily_limit || 5) && (
              <span style={{ color: 'orange' }}> (Rate limited)</span>
            )}
          </dd>

          <dt>Last Execution:</dt>
          <dd>{formatTimestamp(stats?.last_execution_at)}</dd>
        </dl>
      </section>

      {listener.action_type === 'script' && taskExecutions.length > 0 && (
        <section style={{ marginTop: '2rem' }}>
          <h2>Recent Script Executions</h2>
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Created</th>
                  <th>Status</th>
                  <th>Error</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {taskExecutions.map((task, index) => (
                  <tr key={index}>
                    <td>{formatTimestamp(task.created_at)}</td>
                    <td>
                      {task.status === 'done' && <span style={{ color: 'green' }}>✓ Success</span>}
                      {task.status === 'failed' && <span style={{ color: 'red' }}>✗ Failed</span>}
                      {task.status === 'processing' && (
                        <span style={{ color: 'orange' }}>⟳ Processing</span>
                      )}
                      {task.status === 'pending' && (
                        <span style={{ color: 'var(--text-light)' }}>⏳ Pending</span>
                      )}
                      {task.retry_count > 0 && (
                        <>
                          <br />
                          <small>
                            Retry {task.retry_count}/{task.max_retries}
                          </small>
                        </>
                      )}
                    </td>
                    <td>
                      {task.error ? (
                        <code style={{ fontSize: '0.9em', color: 'red' }}>
                          {task.error.length > 100
                            ? `${task.error.substring(0, 100)}...`
                            : task.error}
                        </code>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td>
                      <Link to={`/tasks?internal_id=${task.id}`}>View Task</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section style={{ marginTop: '2rem' }}>
        <h2>Actions</h2>
        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
          <Button onClick={handleToggleEnabled} disabled={updating}>
            {updating ? 'Updating...' : listener.enabled ? 'Disable' : 'Enable'} Listener
          </Button>

          <Button asChild>
            <Link to={`/event-listeners/${listener.id}/edit`}>Edit Listener</Link>
          </Button>

          <Button onClick={handleDelete} variant="destructive">
            Delete Listener
          </Button>
        </div>
      </section>
    </div>
  );
};

export default EventListenerDetail;
