import { ArrowLeft, Loader2, Power, PowerOff, Trash2 } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { PageContainer } from '@/components/layout/PageHeader';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  describeRecurrenceRule,
  formatSourceId,
  formatTimestamp,
  getActionMeta,
  getTypeMeta,
} from '../automationFormat';

const logDev = (...args) => {
  if (import.meta.env.DEV) {
    console.warn(...args);
  }
};

const flattenMatchConditions = (conditions) => {
  if (!conditions || typeof conditions !== 'object') {
    return [];
  }
  const rows = [];
  for (const [key, value] of Object.entries(conditions)) {
    if (typeof value === 'object' && value !== null) {
      for (const [subKey, subValue] of Object.entries(value)) {
        rows.push([`${key}.${subKey}`, JSON.stringify(subValue)]);
      }
    } else {
      rows.push([key, JSON.stringify(value)]);
    }
  }
  return rows;
};

const CodeBlock = ({ children }) => (
  <pre className="max-h-96 overflow-auto rounded-md border bg-muted/50 p-4 font-mono text-sm leading-relaxed">
    <code>{children}</code>
  </pre>
);

const DetailRow = ({ label, children }) => (
  <div className="grid grid-cols-[minmax(0,9rem)_1fr] gap-3 py-2.5 text-sm">
    <dt className="text-muted-foreground">{label}</dt>
    <dd className="min-w-0 break-words font-medium">{children}</dd>
  </div>
);

const Section = ({ eyebrow, title, description, children }) => (
  <Card>
    <CardHeader className="pb-4">
      <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {eyebrow}
      </p>
      <CardTitle className="text-lg">{title}</CardTitle>
      {description ? <CardDescription>{description}</CardDescription> : null}
    </CardHeader>
    <CardContent className="space-y-4">{children}</CardContent>
  </Card>
);

const EventTrigger = ({ automation }) => {
  const conditions = flattenMatchConditions(automation.match_conditions);
  const hasConditions = conditions.length > 0;
  const hasScript = Boolean(automation.condition_script);

  return (
    <Section
      eyebrow="When"
      title={`An event arrives from ${formatSourceId(automation.source_id)}`}
      description={
        hasConditions && hasScript
          ? 'Both the match conditions and the condition script must pass.'
          : !hasConditions && !hasScript
            ? 'No conditions: every event from this source triggers the automation.'
            : null
      }
    >
      <dl className="divide-y">
        <DetailRow label="Event Source">{formatSourceId(automation.source_id)}</DetailRow>
      </dl>

      {hasConditions ? (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">Match conditions</h3>
          <div className="overflow-hidden rounded-md border">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Field</th>
                  <th className="px-3 py-2 font-medium">Equals</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {conditions.map(([field, value]) => (
                  <tr key={field}>
                    <td className="break-all px-3 py-2 font-mono text-xs">{field}</td>
                    <td className="break-all px-3 py-2 font-mono text-xs">{value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {hasScript ? (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">Condition script</h3>
          <CodeBlock>{automation.condition_script}</CodeBlock>
          <p className="text-xs text-muted-foreground">
            Receives the <code>event</code> and returns True to trigger the automation.
          </p>
        </div>
      ) : null}
    </Section>
  );
};

const ScheduleTrigger = ({ automation }) => {
  const summary = describeRecurrenceRule(automation.recurrence_rule);
  return (
    <Section eyebrow="When" title={summary ?? 'On a custom schedule'}>
      <dl className="divide-y">
        <DetailRow label="Recurrence Rule">
          <code className="break-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
            {automation.recurrence_rule}
          </code>
        </DetailRow>
        <DetailRow label="Next Scheduled">
          {automation.enabled
            ? formatTimestamp(automation.next_scheduled_at)
            : 'Paused while disabled'}
        </DetailRow>
      </dl>
    </Section>
  );
};

const ActionSection = ({ automation }) => {
  if (automation.action_type === 'script') {
    const scriptCode = automation.action_config?.script_code;
    return (
      <Section
        eyebrow="Then"
        title="Run a script"
        description={`Times out after ${automation.action_config?.timeout || 600} seconds.`}
      >
        {scriptCode ? (
          <CodeBlock>{scriptCode}</CodeBlock>
        ) : (
          <p className="text-sm text-muted-foreground">No script code defined.</p>
        )}
      </Section>
    );
  }

  const context = automation.action_config?.context;
  const opensNewConversation = automation.action_config?.conversation === 'new';
  return (
    <Section
      eyebrow="Then"
      title="Wake the assistant"
      description={
        opensNewConversation
          ? 'Each run starts a new conversation with the prompt below.'
          : 'The assistant is woken in this conversation with the prompt below.'
      }
    >
      {context ? (
        <blockquote className="whitespace-pre-wrap rounded-md border-l-4 border-primary/40 bg-muted/50 px-4 py-3 text-sm">
          {context}
        </blockquote>
      ) : (
        <p className="text-sm italic text-muted-foreground">The default prompt will be used.</p>
      )}
    </Section>
  );
};

const AutomationDetail = () => {
  const { type, id } = useParams();
  const navigate = useNavigate();
  const [automation, setAutomation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [updating, setUpdating] = useState(false);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const response = await fetch(`/api/automations/${type}/${id}`);
        logDev('[Automations] Fetch automation detail', type, id, response.status);

        if (response.status === 404) {
          setAutomation(null);
        } else if (!response.ok) {
          throw new Error(`Failed to fetch automation: ${response.statusText}`);
        } else {
          setAutomation(await response.json());
        }
      } catch (err) {
        setLoadError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (type && id) {
      fetchData();
    }
  }, [type, id]);

  const handleToggleEnabled = async () => {
    setUpdating(true);
    setActionError(null);
    try {
      const response = await fetch(`/api/automations/${type}/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !automation.enabled }),
      });
      if (!response.ok) {
        throw new Error(`Failed to update automation: ${response.statusText}`);
      }
      setAutomation(await response.json());
    } catch (err) {
      setActionError(err.message);
    } finally {
      setUpdating(false);
    }
  };

  const handleDelete = async () => {
    // eslint-disable-next-line no-alert
    const confirmed = window.confirm(
      'Are you sure you want to delete this automation? This action cannot be undone.'
    );
    if (!confirmed) {
      return;
    }

    setActionError(null);
    try {
      const response = await fetch(`/api/automations/${type}/${id}`, { method: 'DELETE' });
      logDev('[Automations] Delete response', response.status);
      if (!response.ok) {
        throw new Error(`Failed to delete automation: ${response.statusText}`);
      }
      navigate('/automations');
    } catch (err) {
      setActionError(err.message);
      console.error('[Automations] Delete failed', err);
    }
  };

  const backLink = (
    <Link
      to="/automations"
      className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      Back to Automations
    </Link>
  );

  if (loading) {
    return (
      <PageContainer className="max-w-5xl">
        <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" aria-hidden="true" />
          Loading automation...
        </div>
      </PageContainer>
    );
  }

  if (loadError || !automation) {
    return (
      <PageContainer className="max-w-5xl space-y-6">
        {backLink}
        {loadError ? (
          <Alert variant="destructive">
            <AlertDescription>Error: {loadError}</AlertDescription>
          </Alert>
        ) : (
          <div className="rounded-lg border border-dashed px-6 py-16 text-center">
            <h1 className="text-lg font-semibold">Automation not found</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              It may have been deleted, or the link is out of date.
            </p>
          </div>
        )}
      </PageContainer>
    );
  }

  const typeMeta = getTypeMeta(automation.type);
  const actionMeta = getActionMeta(automation.action_type);
  const TypeIcon = typeMeta.icon;
  const ActionIcon = actionMeta.icon;

  return (
    <PageContainer className="max-w-5xl">
      <div className="mb-6 space-y-4">
        {backLink}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 gap-4">
            <div
              className={`flex size-12 shrink-0 items-center justify-center rounded-xl ${
                automation.enabled ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'
              }`}
              aria-hidden="true"
            >
              <TypeIcon className="size-6" />
            </div>
            <div className="min-w-0 space-y-2">
              <h1 className="break-words text-2xl font-bold tracking-tight sm:text-3xl">
                {automation.name}
              </h1>
              <div className="flex flex-wrap gap-2">
                <Badge variant={automation.enabled ? 'default' : 'outline'}>
                  {automation.enabled ? 'Enabled' : 'Disabled'}
                </Badge>
                <Badge variant="secondary" className="gap-1 font-medium">
                  <TypeIcon className="size-3" aria-hidden="true" />
                  {typeMeta.label}
                </Badge>
                <Badge variant="outline" className="gap-1 font-medium">
                  <ActionIcon className="size-3" aria-hidden="true" />
                  {actionMeta.label}
                </Badge>
              </div>
              {automation.description ? (
                <p className="max-w-2xl text-muted-foreground">{automation.description}</p>
              ) : null}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button variant="outline" asChild>
              <Link to={`/artifacts?kind=${automation.type}`}>Review automation trust</Link>
            </Button>
            <Button
              variant="outline"
              className="gap-2"
              onClick={handleToggleEnabled}
              disabled={updating}
            >
              {automation.enabled ? (
                <PowerOff className="size-4" aria-hidden="true" />
              ) : (
                <Power className="size-4" aria-hidden="true" />
              )}
              {updating ? 'Updating...' : automation.enabled ? 'Disable' : 'Enable'} Automation
            </Button>
            <Button
              variant="outline"
              onClick={handleDelete}
              className="gap-2 text-red-600 hover:bg-red-600 hover:text-white dark:text-red-400 dark:hover:bg-red-700 dark:hover:text-white"
            >
              <Trash2 className="size-4" aria-hidden="true" />
              Delete
            </Button>
          </div>
        </div>
      </div>

      {actionError ? (
        <Alert variant="destructive" className="mb-6">
          <AlertDescription>Error: {actionError}</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-6">
          {automation.type === 'event' ? (
            <EventTrigger automation={automation} />
          ) : (
            <ScheduleTrigger automation={automation} />
          )}
          <ActionSection automation={automation} />
        </div>

        <Card className="h-fit">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Details</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="divide-y">
              <DetailRow label="Status">{automation.enabled ? 'Enabled' : 'Disabled'}</DetailRow>
              <DetailRow label="Runs">{automation.execution_count || 0}</DetailRow>
              <DetailRow label="Last run">
                {formatTimestamp(automation.last_execution_at)}
              </DetailRow>
              <DetailRow label="Created">{formatTimestamp(automation.created_at)}</DetailRow>
              <DetailRow label="Conversation">
                <code className="break-all font-mono text-xs">{automation.conversation_id}</code>
              </DetailRow>
              <DetailRow label="Interface">{automation.interface_type}</DetailRow>
              <DetailRow label="ID">{automation.id}</DetailRow>
            </dl>
          </CardContent>
        </Card>
      </div>
    </PageContainer>
  );
};

export default AutomationDetail;
