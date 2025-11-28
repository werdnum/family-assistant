import React, { useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';

const CreateEventAutomation = ({ onSuccess, onCancel }) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [validationErrors, setValidationErrors] = useState({});

  const [formData, setFormData] = useState({
    name: '',
    event_source: 'home_assistant',
    action_type: 'wake_llm',
    description: '',
    condition_type: 'json',
    match_conditions: '{}',
    condition_script: '',
    script_code: '',
    timeout: 600,
    context: '',
  });

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData({
      ...formData,
      [name]: value,
    });

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

    if (formData.condition_type === 'json') {
      try {
        JSON.parse(formData.match_conditions);
      } catch (_e) {
        errors.match_conditions = 'Invalid JSON format';
      }
    }

    if (formData.action_type === 'script' && !formData.script_code.trim()) {
      errors.script_code = 'Script code is required for script actions';
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
      const requestData = {
        name: formData.name,
        source_id: formData.event_source,
        match_conditions:
          formData.condition_type === 'json' ? JSON.parse(formData.match_conditions) : {},
        action_type: formData.action_type,
        action_config: {},
        description: formData.description || null,
        enabled: true,
        one_time: false,
        conversation_id: 'web',
      };

      if (formData.condition_type === 'script' && formData.condition_script) {
        requestData.condition_script = formData.condition_script;
      }

      if (formData.action_type === 'script') {
        requestData.action_config = {
          script_code: formData.script_code,
          timeout: Number(formData.timeout) || 600,
        };
      } else if (formData.context) {
        requestData.action_config.context = formData.context;
      }

      const response = await fetch('/api/automations/event?conversation_id=web', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to create event automation');
      }

      const result = await response.json();
      onSuccess(result.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Create Event Automation</h1>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Event Automation Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <form onSubmit={handleSubmit} className="space-y-6">
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
                placeholder="Optional description for this automation"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="event_source">Event Source</Label>
              <Select
                value={formData.event_source}
                onValueChange={(value) => handleSelectChange('event_source', value)}
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
            </div>

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
                    Define when this automation should trigger
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  <Textarea
                    name="condition_script"
                    value={formData.condition_script}
                    onChange={handleInputChange}
                    rows={6}
                    placeholder="# Return True to trigger the automation\nreturn event.get('entity_id') == 'sensor.example'"
                    className="font-mono"
                  />
                  <p className="text-sm text-muted-foreground">
                    Starlark script that returns True/False
                  </p>
                </div>
              )}
            </div>

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
                    placeholder="# Starlark script to execute\nprint('Event triggered:', event)"
                  />
                  {validationErrors.script_code && (
                    <Alert variant="destructive">
                      <AlertDescription>{validationErrors.script_code}</AlertDescription>
                    </Alert>
                  )}
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
                </div>
              </>
            )}

            {formData.action_type === 'wake_llm' && (
              <div className="space-y-2">
                <Label htmlFor="context">LLM Callback Prompt</Label>
                <Textarea
                  id="context"
                  name="context"
                  value={formData.context}
                  onChange={handleInputChange}
                  rows={3}
                  placeholder="Optional custom prompt for the LLM"
                />
              </div>
            )}

            <div className="flex gap-4 pt-6">
              <Button type="submit" disabled={loading}>
                {loading ? 'Creating...' : 'Create Automation'}
              </Button>
              <Button type="button" variant="secondary" onClick={onCancel}>
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
};

export default CreateEventAutomation;
