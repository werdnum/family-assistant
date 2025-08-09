import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';

const EventListenerForm = ({ isEdit, onSuccess, onCancel }) => {
  const { id } = useParams();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [validationErrors, setValidationErrors] = useState({});

  // Form data state
  const [formData, setFormData] = useState({
    name: '',
    source_id: 'home_assistant',
    action_type: 'wake_llm',
    description: '',
    enabled: true,
    one_time: false,
    condition_type: 'json', // 'json' or 'script'
    match_conditions: '{}',
    condition_script: '',
    script_code: '',
    timeout: 600,
    llm_callback_prompt: '',
    action_parameters: '{}',
  });

  // Load existing listener data for editing
  useEffect(() => {
    if (isEdit && id) {
      const fetchListener = async () => {
        setLoading(true);
        try {
          const response = await fetch(`/api/event-listeners/${id}`);
          if (!response.ok) {
            throw new Error(`Failed to fetch listener: ${response.statusText}`);
          }
          const listener = await response.json();

          setFormData({
            name: listener.name,
            source_id: listener.source_id,
            action_type: listener.action_type,
            description: listener.description || '',
            enabled: listener.enabled,
            one_time: listener.one_time,
            condition_type: listener.condition_script ? 'script' : 'json',
            match_conditions: JSON.stringify(listener.match_conditions, null, 2),
            condition_script: listener.condition_script || '',
            script_code: listener.action_config?.script_code || '',
            timeout: listener.action_config?.timeout || 600,
            llm_callback_prompt: listener.action_config?.llm_callback_prompt || '',
            action_parameters: JSON.stringify(
              listener.action_config?.action_parameters || {},
              null,
              2
            ),
          });
        } catch (err) {
          setError(err.message);
        } finally {
          setLoading(false);
        }
      };

      fetchListener();
    }
  }, [isEdit, id]);

  const handleInputChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData({
      ...formData,
      [name]: type === 'checkbox' ? checked : value,
    });

    // Clear validation error for this field
    if (validationErrors[name]) {
      setValidationErrors({
        ...validationErrors,
        [name]: null,
      });
    }
  };

  const validateForm = () => {
    const errors = {};

    if (!formData.name.trim()) {
      errors.name = 'Name is required';
    }

    // Validate JSON fields
    if (formData.condition_type === 'json') {
      try {
        JSON.parse(formData.match_conditions);
      } catch (_e) {
        errors.match_conditions = 'Invalid JSON format';
      }
    }

    if (formData.action_type === 'script' && !formData.script_code.trim()) {
      errors.script_code = 'Script code is required for script listeners';
    }

    if (formData.action_type === 'wake_llm') {
      try {
        JSON.parse(formData.action_parameters);
      } catch (_e) {
        errors.action_parameters = 'Invalid JSON format';
      }
    }

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!validateForm()) {
      return;
    }

    setLoading(true);
    setError(null);

    try {
      // Prepare request data
      const requestData = {
        name: formData.name,
        source_id: formData.source_id,
        description: formData.description || null,
        enabled: formData.enabled,
        one_time: formData.one_time,
        match_conditions:
          formData.condition_type === 'json' ? JSON.parse(formData.match_conditions) : {},
        action_config: {},
      };

      // Add action_type for new listeners
      if (!isEdit) {
        requestData.action_type = formData.action_type;
        requestData.conversation_id = 'web'; // Default conversation for web-created listeners
      }

      // Configure action_config based on action type
      if (formData.action_type === 'script') {
        requestData.action_config = {
          script_code: formData.script_code,
          timeout: parseInt(formData.timeout, 10),
        };
      } else if (formData.action_type === 'wake_llm') {
        requestData.action_config = {};
        if (formData.llm_callback_prompt) {
          requestData.action_config.llm_callback_prompt = formData.llm_callback_prompt;
        }
        const actionParams = JSON.parse(formData.action_parameters);
        if (Object.keys(actionParams).length > 0) {
          requestData.action_config.action_parameters = actionParams;
        }
      }

      // Add condition script if using script conditions
      if (formData.condition_type === 'script' && formData.condition_script) {
        requestData.condition_script = formData.condition_script;
      }

      let url;
      const method = isEdit ? 'PATCH' : 'POST';

      if (isEdit) {
        url = new window.URL(`/api/event-listeners/${id}`, window.location.origin);
        url.searchParams.set('conversation_id', 'web');
      } else {
        url = '/api/event-listeners';
      }

      const response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to ${isEdit ? 'update' : 'create'} listener`);
      }

      const result = await response.json();
      onSuccess(result.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    onCancel(isEdit ? id : null);
  };

  if (loading && isEdit) {
    return <div>Loading listener data...</div>;
  }

  return (
    <div className="event-listener-form">
      <h1>{isEdit ? `Edit Event Listener: ${formData.name}` : 'Create New Event Listener'}</h1>

      {error && (
        <div className="error" style={{ marginBottom: '1rem' }}>
          Error: {error}
        </div>
      )}

      <form onSubmit={handleSubmit}>
        {/* Basic Information */}
        <div className="form-group">
          <label htmlFor="name">Name *</label>
          <input
            type="text"
            id="name"
            name="name"
            value={formData.name}
            onChange={handleInputChange}
            required
          />
          {validationErrors.name && <div className="validation-error">{validationErrors.name}</div>}
        </div>

        <div className="form-group">
          <label htmlFor="description">Description</label>
          <textarea
            id="description"
            name="description"
            value={formData.description}
            onChange={handleInputChange}
            rows="3"
          />
        </div>

        <div className="form-group">
          <label htmlFor="source_id">Event Source</label>
          <select
            id="source_id"
            name="source_id"
            value={formData.source_id}
            onChange={handleInputChange}
          >
            <option value="home_assistant">Home Assistant</option>
            <option value="indexing">Document Indexing</option>
            <option value="webhook">Webhook</option>
          </select>
        </div>

        <div className="form-group">
          <label htmlFor="action_type">Action Type</label>
          {!isEdit ? (
            <select
              id="action_type"
              name="action_type"
              value={formData.action_type}
              onChange={handleInputChange}
            >
              <option value="wake_llm">LLM Callback</option>
              <option value="script">Script</option>
            </select>
          ) : (
            <div
              style={{
                padding: '0.5rem',
                backgroundColor: 'var(--bg-secondary)',
                borderRadius: '4px',
              }}
            >
              <strong>{formData.action_type === 'wake_llm' ? 'LLM Callback' : 'Script'}</strong>
            </div>
          )}
          <div className="help-text">
            {!isEdit
              ? 'Choose how the listener responds to events'
              : 'Action type cannot be changed after creation'}
          </div>
        </div>

        {/* Trigger Conditions */}
        <div className="form-group">
          <label>Trigger Conditions</label>
          <div style={{ marginBottom: '1rem' }}>
            <input
              type="radio"
              id="conditions-json"
              name="condition_type"
              value="json"
              checked={formData.condition_type === 'json'}
              onChange={handleInputChange}
            />
            <label
              htmlFor="conditions-json"
              style={{ display: 'inline', marginLeft: '0.5rem', fontWeight: 'normal' }}
            >
              JSON Match Conditions
            </label>
          </div>
          <div style={{ marginBottom: '1rem' }}>
            <input
              type="radio"
              id="conditions-script"
              name="condition_type"
              value="script"
              checked={formData.condition_type === 'script'}
              onChange={handleInputChange}
            />
            <label
              htmlFor="conditions-script"
              style={{ display: 'inline', marginLeft: '0.5rem', fontWeight: 'normal' }}
            >
              Starlark Condition Script
            </label>
          </div>

          {formData.condition_type === 'json' ? (
            <div>
              <textarea
                name="match_conditions"
                value={formData.match_conditions}
                onChange={handleInputChange}
                rows="6"
                placeholder={'{\n  "entity_id": "sensor.example",\n  "new_state.state": "on"\n}'}
                style={{ fontFamily: 'monospace' }}
              />
              {validationErrors.match_conditions && (
                <div className="validation-error">{validationErrors.match_conditions}</div>
              )}
              <div className="help-text">
                Define when this listener should trigger. Common fields: entity_id, new_state.state,
                document_type
              </div>
            </div>
          ) : (
            <div>
              <textarea
                name="condition_script"
                value={formData.condition_script}
                onChange={handleInputChange}
                rows="6"
                placeholder="# Return True to trigger the listener\nreturn event.get('entity_id') == 'sensor.example'"
                style={{ fontFamily: 'monospace' }}
              />
              <div className="help-text">
                Starlark script that returns True/False to determine if the listener should trigger.
                The 'event' variable contains the event data.
              </div>
            </div>
          )}
        </div>

        {/* Script Fields */}
        {formData.action_type === 'script' && (
          <>
            <div className="form-group">
              <label htmlFor="script_code">Script Code *</label>
              <textarea
                id="script_code"
                name="script_code"
                value={formData.script_code}
                onChange={handleInputChange}
                rows="10"
                style={{ fontFamily: 'monospace' }}
                placeholder="# Starlark script to execute when triggered\nprint('Event triggered:', event)"
              />
              {validationErrors.script_code && (
                <div className="validation-error">{validationErrors.script_code}</div>
              )}
              <div className="help-text">
                Starlark script that will execute when the listener is triggered
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="timeout">Timeout (seconds)</label>
              <input
                type="number"
                id="timeout"
                name="timeout"
                value={formData.timeout}
                onChange={handleInputChange}
                min="1"
                max="900"
              />
              <div className="help-text">Maximum execution time for the script</div>
            </div>
          </>
        )}

        {/* LLM Fields */}
        {formData.action_type === 'wake_llm' && (
          <>
            <div className="form-group">
              <label htmlFor="llm_callback_prompt">LLM Callback Prompt</label>
              <textarea
                id="llm_callback_prompt"
                name="llm_callback_prompt"
                value={formData.llm_callback_prompt}
                onChange={handleInputChange}
                rows="3"
                placeholder="Optional custom prompt for the LLM response"
              />
              <div className="help-text">
                Optional custom prompt. If not provided, default prompt will be used.
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="action_parameters">Action Parameters (JSON)</label>
              <textarea
                id="action_parameters"
                name="action_parameters"
                value={formData.action_parameters}
                onChange={handleInputChange}
                rows="4"
                style={{ fontFamily: 'monospace' }}
                placeholder={'{\n  "tools": ["search_notes", "send_message"]\n}'}
              />
              {validationErrors.action_parameters && (
                <div className="validation-error">{validationErrors.action_parameters}</div>
              )}
              <div className="help-text">
                Configure how the LLM responds. Common fields: tools (array of tool names)
              </div>
            </div>
          </>
        )}

        {/* Checkboxes */}
        <div className="checkbox-group">
          <input
            type="checkbox"
            id="one_time"
            name="one_time"
            checked={formData.one_time}
            onChange={handleInputChange}
          />
          <label htmlFor="one_time">One-time listener (auto-disable after first trigger)</label>
        </div>

        <div className="checkbox-group">
          <input
            type="checkbox"
            id="enabled"
            name="enabled"
            checked={formData.enabled}
            onChange={handleInputChange}
          />
          <label htmlFor="enabled">Enabled</label>
        </div>

        {/* Actions */}
        <div className="form-actions">
          <Button type="submit" disabled={loading}>
            {loading ? 'Saving...' : isEdit ? 'Save Changes' : 'Create Listener'}
          </Button>
          <Button type="button" variant="secondary" onClick={handleCancel}>
            Cancel
          </Button>
        </div>
      </form>

      <style jsx>{`
        .form-group {
          margin-bottom: 1.5rem;
        }

        .form-group label {
          display: block;
          font-weight: bold;
          margin-bottom: 0.5rem;
        }

        .form-group input[type="text"],
        .form-group textarea,
        .form-group input[type="number"],
        .form-group select {
          width: 100%;
          padding: 0.5rem;
          border: 1px solid var(--border);
          border-radius: 4px;
          background-color: var(--bg);
          color: var(--text);
        }

        .form-group textarea {
          min-height: 100px;
          resize: vertical;
        }

        .checkbox-group {
          margin: 1rem 0;
        }

        .checkbox-group label {
          display: inline;
          font-weight: normal;
          margin-left: 0.5rem;
        }

        .form-actions {
          margin-top: 2rem;
          display: flex;
          gap: 1rem;
        }

        .help-text {
          font-size: 0.9em;
          color: var(--text-light);
          margin-top: 0.25rem;
        }

        .validation-error {
          color: red;
          margin-top: 0.5rem;
          font-weight: bold;
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

export default EventListenerForm;
