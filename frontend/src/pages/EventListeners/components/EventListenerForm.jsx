import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';

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

  const handleSelectChange = (name, value) => {
    setFormData({
      ...formData,
      [name]: value,
    });

    // Clear validation error for this field
    if (validationErrors[name]) {
      setValidationErrors({
        ...validationErrors,
        [name]: null,
      });
    }
  };

  const handleCheckboxChange = (name, checked) => {
    setFormData({
      ...formData,
      [name]: checked,
    });
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
    return <div className="flex items-center justify-center p-8">Loading listener data...</div>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">
        {isEdit ? `Edit Event Listener: ${formData.name}` : 'Create New Event Listener'}
      </h1>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Event Listener Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <form onSubmit={handleSubmit} className="space-y-6">
            {/* Basic Information */}
            <div className="space-y-2">
              <Label htmlFor="name">Name *</Label>
              <Input
                id="name"
                name="name"
                value={formData.name}
                onChange={handleInputChange}
                required
              />
              {validationErrors.name && (
                <Alert variant="destructive" className="mt-2">
                  <AlertDescription>{validationErrors.name}</AlertDescription>
                </Alert>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                name="description"
                value={formData.description}
                onChange={handleInputChange}
                rows={3}
                placeholder="Optional description for this event listener"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="source_id">Event Source</Label>
              <Select
                value={formData.source_id}
                onValueChange={(value) => handleSelectChange('source_id', value)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="home_assistant">Home Assistant</SelectItem>
                  <SelectItem value="indexing">Document Indexing</SelectItem>
                  <SelectItem value="webhook">Webhook</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="action_type">Action Type</Label>
              {!isEdit ? (
                <Select
                  value={formData.action_type}
                  onValueChange={(value) => handleSelectChange('action_type', value)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="wake_llm">LLM Callback</SelectItem>
                    <SelectItem value="script">Script</SelectItem>
                  </SelectContent>
                </Select>
              ) : (
                <div className="px-3 py-2 bg-muted rounded-md">
                  <strong>{formData.action_type === 'wake_llm' ? 'LLM Callback' : 'Script'}</strong>
                </div>
              )}
              <p className="text-sm text-muted-foreground">
                {!isEdit
                  ? 'Choose how the listener responds to events'
                  : 'Action type cannot be changed after creation'}
              </p>
            </div>

            {/* Trigger Conditions */}
            <div className="space-y-4">
              <Label>Trigger Conditions</Label>
              <RadioGroup
                value={formData.condition_type}
                onValueChange={(value) => handleSelectChange('condition_type', value)}
                className="space-y-3"
              >
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="json" id="conditions-json" />
                  <Label htmlFor="conditions-json" className="font-normal">
                    JSON Match Conditions
                  </Label>
                </div>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="script" id="conditions-script" />
                  <Label htmlFor="conditions-script" className="font-normal">
                    Starlark Condition Script
                  </Label>
                </div>
              </RadioGroup>

              {formData.condition_type === 'json' ? (
                <div className="space-y-2">
                  <Textarea
                    name="match_conditions"
                    value={formData.match_conditions}
                    onChange={handleInputChange}
                    rows={6}
                    placeholder='{\n  "entity_id": "sensor.example",\n  "new_state.state": "on"\n}'
                    className="font-mono"
                  />
                  {validationErrors.match_conditions && (
                    <Alert variant="destructive">
                      <AlertDescription>{validationErrors.match_conditions}</AlertDescription>
                    </Alert>
                  )}
                  <p className="text-sm text-muted-foreground">
                    Define when this listener should trigger. Common fields: entity_id,
                    new_state.state, document_type
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  <Textarea
                    name="condition_script"
                    value={formData.condition_script}
                    onChange={handleInputChange}
                    rows={6}
                    placeholder="# Return True to trigger the listener\nreturn event.get('entity_id') == 'sensor.example'"
                    className="font-mono"
                  />
                  <p className="text-sm text-muted-foreground">
                    Starlark script that returns True/False to determine if the listener should
                    trigger. The 'event' variable contains the event data.
                  </p>
                </div>
              )}
            </div>

            {/* Script Fields */}
            {formData.action_type === 'script' && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="script_code">Script Code *</Label>
                  <Textarea
                    id="script_code"
                    name="script_code"
                    value={formData.script_code}
                    onChange={handleInputChange}
                    rows={10}
                    className="font-mono"
                    placeholder="# Starlark script to execute when triggered\nprint('Event triggered:', event)"
                  />
                  {validationErrors.script_code && (
                    <Alert variant="destructive">
                      <AlertDescription>{validationErrors.script_code}</AlertDescription>
                    </Alert>
                  )}
                  <p className="text-sm text-muted-foreground">
                    Starlark script that will execute when the listener is triggered
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="timeout">Timeout (seconds)</Label>
                  <Input
                    type="number"
                    id="timeout"
                    name="timeout"
                    value={formData.timeout}
                    onChange={handleInputChange}
                    min={1}
                    max={900}
                  />
                  <p className="text-sm text-muted-foreground">
                    Maximum execution time for the script
                  </p>
                </div>
              </>
            )}

            {/* LLM Fields */}
            {formData.action_type === 'wake_llm' && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="llm_callback_prompt">LLM Callback Prompt</Label>
                  <Textarea
                    id="llm_callback_prompt"
                    name="llm_callback_prompt"
                    value={formData.llm_callback_prompt}
                    onChange={handleInputChange}
                    rows={3}
                    placeholder="Optional custom prompt for the LLM response"
                  />
                  <p className="text-sm text-muted-foreground">
                    Optional custom prompt. If not provided, default prompt will be used.
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="action_parameters">Action Parameters (JSON)</Label>
                  <Textarea
                    id="action_parameters"
                    name="action_parameters"
                    value={formData.action_parameters}
                    onChange={handleInputChange}
                    rows={4}
                    className="font-mono"
                    placeholder='{\n  "tools": ["search_notes", "send_message"]\n}'
                  />
                  {validationErrors.action_parameters && (
                    <Alert variant="destructive">
                      <AlertDescription>{validationErrors.action_parameters}</AlertDescription>
                    </Alert>
                  )}
                  <p className="text-sm text-muted-foreground">
                    Configure how the LLM responds. Common fields: tools (array of tool names)
                  </p>
                </div>
              </>
            )}

            {/* Checkboxes */}
            <div className="space-y-4">
              <div className="flex items-center space-x-2">
                <Checkbox
                  id="one_time"
                  checked={formData.one_time}
                  onCheckedChange={(checked) => handleCheckboxChange('one_time', checked)}
                />
                <Label htmlFor="one_time" className="font-normal">
                  One-time listener (auto-disable after first trigger)
                </Label>
              </div>

              <div className="flex items-center space-x-2">
                <Checkbox
                  id="enabled"
                  checked={formData.enabled}
                  onCheckedChange={(checked) => handleCheckboxChange('enabled', checked)}
                />
                <Label htmlFor="enabled" className="font-normal">
                  Enabled
                </Label>
              </div>
            </div>

            {/* Actions */}
            <div className="flex gap-4 pt-6">
              <Button type="submit" disabled={loading}>
                {loading ? 'Saving...' : isEdit ? 'Save Changes' : 'Create Listener'}
              </Button>
              <Button type="button" variant="secondary" onClick={handleCancel}>
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
};

export default EventListenerForm;
