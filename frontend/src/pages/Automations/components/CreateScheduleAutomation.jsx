import React, { useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';

const logDev = (...args) => {
  if (import.meta.env.DEV) {
    console.warn(...args);
  }
};

const CreateScheduleAutomation = ({ onSuccess, onCancel }) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [validationErrors, setValidationErrors] = useState({});

  const [formData, setFormData] = useState({
    name: '',
    action_type: 'wake_llm',
    description: '',
    recurrence_rule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
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

    if (!formData.recurrence_rule.trim()) {
      errors.recurrence_rule = 'Recurrence rule is required';
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
        recurrence_rule: formData.recurrence_rule,
        action_type: formData.action_type,
        action_config: {},
        description: formData.description || null,
        enabled: true,
        conversation_id: 'web',
      };

      if (formData.action_type === 'script') {
        requestData.action_config = {
          script_code: formData.script_code,
          timeout: Number(formData.timeout) || 600,
        };
      } else if (formData.context) {
        requestData.action_config.context = formData.context;
      }

      logDev('[Automations] Submitting schedule automation', requestData);

      const response = await fetch('/api/automations/schedule?conversation_id=web', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestData),
      });

      logDev('[Automations] Schedule create response status', response.status);
      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to create schedule automation');
      }

      const result = await response.json();
      logDev('[Automations] Schedule create result', result);
      onSuccess(result.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Create Schedule Automation</h1>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Schedule Automation Configuration</CardTitle>
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
              <Label htmlFor="recurrence_rule">Recurrence Rule (RRULE) *</Label>
              <Input
                id="recurrence_rule"
                name="recurrence_rule"
                value={formData.recurrence_rule}
                onChange={handleInputChange}
                required
                placeholder="FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
              />
              {validationErrors.recurrence_rule && (
                <Alert variant="destructive" className="mt-2">
                  <AlertDescription>{validationErrors.recurrence_rule}</AlertDescription>
                </Alert>
              )}
              <p className="text-sm text-muted-foreground">
                Examples: FREQ=DAILY;BYHOUR=9 (daily at 9am), FREQ=WEEKLY;BYDAY=MO (every Monday)
              </p>
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
                    placeholder="# Python script to execute on schedule\nprint('Scheduled task executed')"
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
                <p className="text-sm text-muted-foreground">
                  For example: &quot;Generate a daily summary of my notes&quot;
                </p>
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

export default CreateScheduleAutomation;
