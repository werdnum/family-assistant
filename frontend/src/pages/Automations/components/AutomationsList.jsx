import { CalendarClock, ChevronRight, Loader2, Plus, Workflow, Zap } from 'lucide-react';
import React, { useCallback, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageContainer, PageHeader } from '@/components/layout/PageHeader';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { NativeSelect } from '@/components/ui/native-select';
import { Switch } from '@/components/ui/switch';
import {
  automationPath,
  describeRecurrenceRule,
  formatSourceId,
  formatTimestamp,
  getActionMeta,
  getTypeMeta,
} from '../automationFormat';

const MetaItem = ({ label, children }) => (
  <div className="min-w-0">
    <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
    <dd className="mt-0.5 truncate text-sm">{children}</dd>
  </div>
);

const AutomationCard = ({ automation, onToggle, toggling }) => {
  const typeMeta = getTypeMeta(automation.type);
  const actionMeta = getActionMeta(automation.action_type);
  const TypeIcon = typeMeta.icon;
  const ActionIcon = actionMeta.icon;
  const scheduleSummary =
    automation.type === 'schedule' ? describeRecurrenceRule(automation.recurrence_rule) : null;
  const switchId = `automation-enabled-${automation.type}-${automation.id}`;

  return (
    <Card
      className={`transition-colors hover:border-foreground/20 ${
        automation.enabled ? '' : 'bg-muted/40'
      }`}
      data-testid="automation-card"
      data-automation-name={automation.name}
    >
      <div className="flex gap-4 p-4 sm:p-5">
        <div
          className={`flex size-10 shrink-0 items-center justify-center rounded-lg ${
            automation.enabled ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'
          }`}
          aria-hidden="true"
        >
          <TypeIcon className="size-5" />
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <h3 className="break-words text-base font-semibold leading-tight">
                <Link
                  to={automationPath(automation)}
                  className="hover:underline focus-visible:underline"
                >
                  {automation.name}
                </Link>
              </h3>
              {automation.description ? (
                <p className="line-clamp-2 text-sm text-muted-foreground">
                  {automation.description}
                </p>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Label
                htmlFor={switchId}
                className={`hidden text-xs font-medium sm:inline ${
                  automation.enabled ? 'text-foreground' : 'text-muted-foreground'
                }`}
              >
                {automation.enabled ? 'Enabled' : 'Disabled'}
              </Label>
              <Switch
                id={switchId}
                checked={automation.enabled}
                disabled={toggling}
                onCheckedChange={() => onToggle(automation)}
                aria-label={`${automation.enabled ? 'Disable' : 'Enable'} ${automation.name}`}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" className="gap-1 font-medium">
              <TypeIcon className="size-3" aria-hidden="true" />
              {typeMeta.shortLabel}
            </Badge>
            <Badge variant="outline" className="gap-1 font-medium">
              <ActionIcon className="size-3" aria-hidden="true" />
              {actionMeta.label}
            </Badge>
            {automation.type === 'event' && automation.source_id ? (
              <span className="text-sm text-muted-foreground">
                on {formatSourceId(automation.source_id)} events
              </span>
            ) : null}
            {automation.type === 'schedule' && automation.recurrence_rule ? (
              scheduleSummary ? (
                <span className="text-sm text-muted-foreground" title={automation.recurrence_rule}>
                  {scheduleSummary}
                </span>
              ) : (
                <code className="break-all rounded bg-muted px-1.5 py-0.5 text-xs">
                  {automation.recurrence_rule}
                </code>
              )
            ) : null}
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t pt-3 sm:grid-cols-4">
            {automation.type === 'schedule' ? (
              <MetaItem label="Next run">
                {automation.enabled && automation.next_scheduled_at
                  ? formatTimestamp(automation.next_scheduled_at)
                  : '—'}
              </MetaItem>
            ) : null}
            <MetaItem label="Last run">{formatTimestamp(automation.last_execution_at)}</MetaItem>
            <MetaItem label="Runs">{automation.execution_count || 0}</MetaItem>
            <MetaItem label="Conversation">
              <span className="font-mono text-xs">{automation.conversation_id}</span>
            </MetaItem>
          </dl>
        </div>

        <Link
          to={automationPath(automation)}
          className="hidden self-center rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground sm:block"
          aria-label={`View details for ${automation.name}`}
        >
          <ChevronRight className="size-5" aria-hidden="true" />
        </Link>
      </div>
    </Card>
  );
};

const AutomationsList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [automations, setAutomations] = useState([]);
  const [conversationIds, setConversationIds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [togglingKey, setTogglingKey] = useState(null);

  const currentType = searchParams.get('type') || 'all';
  const currentEnabled = searchParams.get('enabled') || '';
  const currentConversation = searchParams.get('conversation') || 'all';
  const filtersActive =
    currentType !== 'all' || currentEnabled !== '' || currentConversation !== 'all';

  const updateFilter = (key, value, defaultValue) => {
    const nextParams = new URLSearchParams(searchParams);
    if (value && value !== defaultValue) {
      nextParams.set(key, value);
    } else {
      nextParams.delete(key);
    }
    setSearchParams(nextParams);
  };

  const fetchAutomations = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (currentType !== 'all') {
        params.append('automation_type', currentType);
      }
      if (currentEnabled) {
        params.append('enabled', currentEnabled);
      }
      if (currentConversation !== 'all') {
        params.append('conversation_id', currentConversation);
      }

      const response = await fetch(`/api/automations?${params}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch automations: ${response.statusText}`);
      }
      const data = await response.json();
      setAutomations(data.automations || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [currentType, currentEnabled, currentConversation]);

  useEffect(() => {
    fetchAutomations();
  }, [fetchAutomations]);

  // The conversation filter offers every conversation that has automations, not just the ones in
  // the currently filtered list, so it is fetched separately and unfiltered.
  useEffect(() => {
    const fetchConversationIds = async () => {
      try {
        const response = await fetch('/api/automations');
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        const ids = new Set(
          (data.automations || [])
            .map((a) => a.conversation_id)
            .filter((id) => id !== null && id !== undefined)
        );
        setConversationIds(Array.from(ids).sort());
      } catch (_err) {
        // Only the conversation dropdown depends on this; the main list reports its own errors.
      }
    };
    fetchConversationIds();
  }, []);

  const toggleEnabled = async (automation) => {
    const key = `${automation.type}-${automation.id}`;
    setTogglingKey(key);
    try {
      const response = await fetch(`/api/automations/${automation.type}/${automation.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !automation.enabled }),
      });
      if (!response.ok) {
        throw new Error('Failed to update automation');
      }
      await fetchAutomations();
    } catch (err) {
      setError(err.message);
    } finally {
      setTogglingKey(null);
    }
  };

  const hasAutomations = automations.length > 0;

  return (
    <PageContainer>
      <PageHeader
        title="Automations"
        description="Let Family Assistant act on its own when something happens or on a regular schedule."
        actions={
          <>
            <Button asChild variant="outline" className="gap-2">
              <Link to="/automations/create/event">
                <Zap className="size-4" aria-hidden="true" />
                Create Event Automation
              </Link>
            </Button>
            <Button asChild className="gap-2">
              <Link to="/automations/create/schedule">
                <CalendarClock className="size-4" aria-hidden="true" />
                Create Schedule Automation
              </Link>
            </Button>
          </>
        }
      />

      <section
        aria-label="Filters"
        className="mb-4 grid grid-cols-2 gap-3 rounded-lg border bg-card p-4 sm:flex sm:flex-wrap sm:items-end"
      >
        <h2 className="sr-only">Filters</h2>
        <div className="space-y-1.5 sm:w-44">
          <Label htmlFor="type" className="text-xs text-muted-foreground">
            Type
          </Label>
          <NativeSelect
            name="type"
            id="type"
            value={currentType}
            onChange={(e) => updateFilter('type', e.target.value, 'all')}
          >
            <option value="all">All types</option>
            <option value="event">Event-based</option>
            <option value="schedule">Schedule-based</option>
          </NativeSelect>
        </div>

        <div className="space-y-1.5 sm:w-44">
          <Label htmlFor="enabled" className="text-xs text-muted-foreground">
            Status
          </Label>
          <NativeSelect
            name="enabled"
            id="enabled"
            value={currentEnabled}
            onChange={(e) => updateFilter('enabled', e.target.value, '')}
          >
            <option value="">Any status</option>
            <option value="true">Enabled only</option>
            <option value="false">Disabled only</option>
          </NativeSelect>
        </div>

        {conversationIds.length > 0 ? (
          <div className="col-span-2 space-y-1.5 sm:w-56">
            <Label htmlFor="conversation" className="text-xs text-muted-foreground">
              Conversation
            </Label>
            <NativeSelect
              name="conversation"
              id="conversation"
              value={currentConversation}
              onChange={(e) => updateFilter('conversation', e.target.value, 'all')}
            >
              <option value="all">All conversations</option>
              {conversationIds.map((conv) => (
                <option key={conv} value={conv}>
                  {conv}
                </option>
              ))}
            </NativeSelect>
          </div>
        ) : null}

        <div className="col-span-2 flex items-center justify-between gap-3 sm:ml-auto">
          {!loading ? (
            <span className="text-sm text-muted-foreground" aria-live="polite">
              Found {automations.length} automation{automations.length !== 1 ? 's' : ''}
            </span>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            onClick={() => setSearchParams({})}
            disabled={!filtersActive}
          >
            Clear Filters
          </Button>
        </div>
      </section>

      {error ? (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      ) : null}

      {loading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" aria-hidden="true" />
          <span>Loading automations...</span>
        </div>
      ) : null}

      {!loading && hasAutomations ? (
        <div className="grid gap-3">
          {automations.map((automation) => (
            <AutomationCard
              key={`${automation.type}-${automation.id}`}
              automation={automation}
              onToggle={toggleEnabled}
              toggling={togglingKey === `${automation.type}-${automation.id}`}
            />
          ))}
        </div>
      ) : null}

      {!loading && !error && !hasAutomations ? (
        <div className="flex flex-col items-center rounded-lg border border-dashed px-6 py-16 text-center">
          <div className="mb-4 flex size-12 items-center justify-center rounded-full bg-muted">
            <Workflow className="size-6 text-muted-foreground" aria-hidden="true" />
          </div>
          {filtersActive ? (
            <>
              <h2 className="text-lg font-semibold">No automations match these filters</h2>
              <p className="mt-1 max-w-md text-sm text-muted-foreground">
                Try a different type or status, or clear the filters to see everything.
              </p>
            </>
          ) : (
            <>
              <h2 className="text-lg font-semibold">No automations yet</h2>
              <p className="mt-1 max-w-md text-sm text-muted-foreground">
                Automations let Family Assistant follow through on routines by itself: reacting to
                events from your home, or running on a schedule. You can also ask for one in chat.
              </p>
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                <Button asChild className="gap-2">
                  <Link to="/automations/create/schedule">
                    <Plus className="size-4" aria-hidden="true" />
                    New schedule automation
                  </Link>
                </Button>
                <Button asChild variant="outline" className="gap-2">
                  <Link to="/docs/automations.md">Learn more</Link>
                </Button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </PageContainer>
  );
};

export default AutomationsList;
