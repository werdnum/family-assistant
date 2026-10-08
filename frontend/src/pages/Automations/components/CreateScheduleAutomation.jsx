import { ArrowLeft } from 'lucide-react';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { PageContainer, PageHeader } from '@/components/layout/PageHeader';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
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
import { describeRecurrenceRule } from '../automationFormat';

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
    new_conversation: false,
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
      } else {
        if (formData.context) {
          requestData.action_config.context = formData.context;
        }
        if (formData.new_conversation) {
          requestData.action_config.conversation = 'new';
        }
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

  const recurrenceSummary = describeRecurrenceRule(formData.recurrence_rule);

  return (
    <PageContainer className="max-w-3xl space-y-6">
      <PageHeader
        className="mb-0"
        title="Create Schedule Automation"
        description="Run an action on a recurring schedule."
        eyebrow={
          <Link
            to="/automations"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            Back to Automations
          </Link>
        }
      />

      {error && (
        <Alert variant="destructive">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardContent className="space-y-6 pt-6">
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
              {recurrenceSummary ? (
                <p className="text-sm font-medium">Runs: {recurrenceSummary}</p>
              ) : null}
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
                <SelectTrigger id="action_type">
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
              <>
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
                <div className="space-y-2">
                  <div className="flex items-center space-x-2">
                    <Checkbox
                      id="new_conversation"
                      checked={formData.new_conversation}
                      onCheckedChange={(checked) =>
                        setFormData({ ...formData, new_conversation: checked === true })
                      }
                    />
                    <Label htmlFor="new_conversation" className="font-normal">
                      Start a new conversation each time
                    </Label>
                  </div>
                  <p className="text-sm text-muted-foreground pl-6">
                    Each run opens its own conversation instead of continuing an existing one, so
                    the prompt should say everything the assistant needs.
                  </p>
                </div>
              </>
            )}

            <div className="flex flex-wrap justify-end gap-2 border-t pt-6">
              <Button type="button" variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
              <Button type="submit" disabled={loading}>
                {loading ? 'Creating...' : 'Create Automation'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageContainer>
  );
};

export default CreateScheduleAutomation;
