import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';

const logDev = (...args) => {
  if (import.meta.env.DEV) {
    console.warn(...args);
  }
};

const AutomationDetail = () => {
  const { type, id } = useParams();
  const navigate = useNavigate();
  const [automation, setAutomation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [updating, setUpdating] = useState(false);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`/api/automations/${type}/${id}`);
        logDev('[Automations] Fetch automation detail', type, id, response.status);

        if (response.status === 404) {
          setAutomation(null);
        } else if (!response.ok) {
          throw new Error(`Failed to fetch automation: ${response.statusText}`);
        } else {
          const data = await response.json();
          logDev('[Automations] Automation detail loaded', data);
          setAutomation(data);
        }
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (type && id) {
      fetchData();
    }
  }, [type, id]);

  const handleToggleEnabled = async () => {
    if (!automation) {
      return;
    }

    setUpdating(true);
    try {
      const response = await fetch(`/api/automations/${type}/${id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          enabled: !automation.enabled,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to update automation: ${response.statusText}`);
      }

      const updatedAutomation = await response.json();
      logDev(
        '[Automations] Toggle enabled success',
        updatedAutomation.id,
        updatedAutomation.enabled
      );
      setAutomation(updatedAutomation);
    } catch (err) {
      setError(err.message);
    } finally {
      setUpdating(false);
    }
  };

  const handleDelete = async () => {
    if (!automation) {
      return;
    }

    // eslint-disable-next-line no-alert
    const confirmed = window.confirm(
      'Are you sure you want to delete this automation? This action cannot be undone.'
    );

    if (!confirmed) {
      return;
    }

    try {
      const response = await fetch(`/api/automations/${type}/${id}`, {
        method: 'DELETE',
      });

      logDev('[Automations] Delete response', response.status);
      if (!response.ok) {
        throw new Error(`Failed to delete automation: ${response.statusText}`);
      }

      logDev('[Automations] Delete success, navigating to list');
      navigate('/automations');
    } catch (err) {
      setError(err.message);
      console.error('[Automations] Delete failed', err);
    }
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Never';
    }
    return new Date(timestamp).toLocaleString();
  };

  const getActionIcon = (actionType) => {
    return actionType === 'wake_llm' ? '🤖' : '📜';
  };

  const getActionTitle = (actionType) => {
    return actionType === 'wake_llm' ? 'LLM Callback' : 'Script Execution';
  };

  const getTypeIcon = (type) => {
    return type === 'event' ? '⚡' : '📅';
  };

  const getTypeTitle = (type) => {
    return type === 'event' ? 'Event-Based' : 'Schedule-Based';
  };

  const formatSourceId = (sourceId) => {
    if (!sourceId) {
      return '';
    }
    return sourceId.replace('_', ' ').replace(/\b\w/g, (l) => l.toUpperCase());
  };

  const formatMatchConditions = (conditions) => {
    if (!conditions || typeof conditions !== 'object') {
      return [];
    }

    const formatted = [];
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
    return <div>Loading automation...</div>;
  }
  if (error) {
    return <div className="error">Error: {error}</div>;
  }
  if (!automation) {
    return <div>Automation not found</div>;
  }

  const formattedConditions =
    automation.type === 'event' && automation.match_conditions
      ? formatMatchConditions(automation.match_conditions)
      : [];

  return (
    <div className="automation-detail">
      <h1>
        {getTypeIcon(automation.type)} {getActionIcon(automation.action_type)} {automation.name}
      </h1>

      <nav style={{ marginBottom: '2rem' }}>
        <Link to="/automations">← Back to Automations</Link>
      </nav>

      {error && (
        <div className="error" style={{ marginBottom: '1rem' }}>
          Error: {error}
        </div>
      )}

      {/* Configuration Section */}
      <section>
        <h2>Configuration</h2>
        <dl>
          <dt>ID:</dt>
          <dd>{automation.id}</dd>

          <dt>Type:</dt>
          <dd>{getTypeTitle(automation.type)}</dd>

          <dt>Description:</dt>
          <dd>{automation.description || 'No description provided'}</dd>

          <dt>Action Type:</dt>
          <dd>{getActionTitle(automation.action_type)}</dd>

          <dt>Status:</dt>
          <dd>
            {automation.enabled ? (
              <span style={{ color: 'var(--accent)' }}>✓ Enabled</span>
            ) : (
              <span style={{ color: 'var(--text-light)' }}>✗ Disabled</span>
            )}
          </dd>

          <dt>Created:</dt>
          <dd>{formatTimestamp(automation.created_at)}</dd>

          <dt>Conversation ID:</dt>
          <dd>
            <code>{automation.conversation_id}</code>
          </dd>

          <dt>Interface Type:</dt>
          <dd>{automation.interface_type}</dd>
        </dl>
      </section>

      {/* Trigger Configuration */}
      <section style={{ marginTop: '2rem' }}>
        <h2>Trigger Configuration</h2>

        {automation.type === 'event' ? (
          <>
            <dl>
              <dt>Event Source:</dt>
              <dd>{formatSourceId(automation.source_id)}</dd>
            </dl>

            <h3>Trigger Conditions</h3>
            <p style={{ color: 'var(--text-light)' }}>
              Events must match these conditions to trigger this automation.
            </p>

            {automation.condition_script ? (
              <>
                <h4>Condition Script (Python)</h4>
                <div
                  style={{
                    backgroundColor: 'var(--bg-secondary)',
                    padding: '1rem',
                    borderRadius: '8px',
                    overflowX: 'auto',
                  }}
                >
                  <pre>
                    <code>{automation.condition_script}</code>
                  </pre>
                </div>
                <p style={{ color: 'var(--text-light)', fontSize: '0.9em', marginTop: '0.5rem' }}>
                  This script receives an 'event' variable and must return True/False to determine
                  if the automation triggers.
                </p>
              </>
            ) : formattedConditions.length > 0 ? (
              <>
                <h4>JSON Match Conditions</h4>
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
          </>
        ) : automation.type === 'schedule' ? (
          <>
            <dl>
              <dt>Recurrence Rule:</dt>
              <dd>
                <code>{automation.recurrence_rule}</code>
              </dd>

              <dt>Next Scheduled:</dt>
              <dd>{formatTimestamp(automation.next_scheduled_at)}</dd>
            </dl>
          </>
        ) : (
          <p>
            <em>No trigger configuration available.</em>
          </p>
        )}
      </section>

      {/* Action Configuration */}
      <section style={{ marginTop: '2rem' }}>
        {automation.action_type === 'script' ? (
          <>
            <h2>Script Code</h2>
            {automation.action_config?.script_code ? (
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
                    <code>{automation.action_config.script_code}</code>
                  </pre>
                </div>
                <p style={{ marginTop: '1rem' }}>
                  <strong>Timeout:</strong> {automation.action_config.timeout || 600} seconds
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
                {automation.action_config?.context ? (
                  <pre
                    style={{
                      backgroundColor: 'var(--bg-secondary)',
                      padding: '1rem',
                      borderRadius: '8px',
                    }}
                  >
                    {automation.action_config.context}
                  </pre>
                ) : (
                  <em>Default prompt will be used</em>
                )}
              </dd>
            </dl>
          </>
        )}
      </section>

      {/* Execution Statistics */}
      <section style={{ marginTop: '2rem' }}>
        <h2>Execution Statistics</h2>
        <dl>
          <dt>Total Executions:</dt>
          <dd>{automation.execution_count || 0}</dd>

          <dt>Last Execution:</dt>
          <dd>{formatTimestamp(automation.last_execution_at)}</dd>
        </dl>
      </section>

      {/* Actions */}
      <section style={{ marginTop: '2rem' }}>
        <h2>Actions</h2>
        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
          {/* Toggle Enable/Disable */}
          <Button onClick={handleToggleEnabled} disabled={updating}>
            {updating ? 'Updating...' : automation.enabled ? 'Disable' : 'Enable'} Automation
          </Button>

          {/* Delete */}
          <Button onClick={handleDelete} variant="destructive">
            Delete Automation
          </Button>
        </div>
      </section>

      <style jsx>{`
        dl {
          display: grid;
          grid-template-columns: auto 1fr;
          gap: 0.5rem 1rem;
        }

        dt {
          font-weight: bold;
          text-align: right;
        }

        dd {
          margin: 0;
        }

        code {
          background-color: var(--bg-secondary);
          padding: 0.2rem 0.4rem;
          border-radius: 3px;
        }

        pre code {
          background: none;
          padding: 0;
        }

        .error {
          color: red;
          background-color: var(--bg-error);
          padding: 1rem;
          border-radius: 4px;
          border: 1px solid red;
        }
      `}</style>
    </div>
  );
};

export default AutomationDetail;
