import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Card, CardFooter, CardHeader } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { ArrowRight, Bot, CalendarClock, Filter, Loader2, ScrollText, Zap } from 'lucide-react';
import styles from './AutomationsList.module.css';

const CREATION_OPTIONS = [
  {
    id: 'event',
    title: 'Event automation',
    description:
      'React instantly when Family Assistant detects an event, alert, or webhook from your tools.',
    icon: Zap,
    to: '/automations/create/event',
    ctaLabel: 'Create Event Automation',
  },
  {
    id: 'schedule',
    title: 'Schedule automation',
    description: 'Run recurring workflows on a dependable cadence using flexible schedule rules.',
    icon: CalendarClock,
    to: '/automations/create/schedule',
    ctaLabel: 'Create Schedule Automation',
  },
];

const TYPE_METADATA = {
  event: { label: 'Event-Based', icon: Zap },
  schedule: { label: 'Schedule-Based', icon: CalendarClock },
};

const ACTION_METADATA = {
  wake_llm: { label: 'LLM Callback', icon: Bot },
  run_script: { label: 'Script Execution', icon: ScrollText },
};

const AutomationsList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [automations, setAutomations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [isFiltersOpen, setIsFiltersOpen] = useState(true);

  // Get current filter values from URL params
  const currentType = searchParams.get('type') || 'all';
  const currentEnabled = searchParams.get('enabled') || '';
  const currentConversation = searchParams.get('conversation') || 'all';

  // Form state for filters
  const [filters, setFilters] = useState({
    type: currentType,
    enabled: currentEnabled,
    conversation: currentConversation,
  });

  const updateSearchParams = (typeValue, enabledValue, conversationValue) => {
    const newParams = new URLSearchParams();
    if (typeValue && typeValue !== 'all') {
      newParams.set('type', typeValue);
    }
    if (enabledValue) {
      newParams.set('enabled', enabledValue);
    }
    if (conversationValue && conversationValue !== 'all') {
      newParams.set('conversation', conversationValue);
    }
    setSearchParams(newParams);
  };

  useEffect(() => {
    setFilters({ type: currentType, enabled: currentEnabled, conversation: currentConversation });
  }, [currentType, currentEnabled, currentConversation]);

  const fetchAutomations = async (type, enabled, conversation) => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();

      if (type && type !== 'all') {
        params.append('automation_type', type);
      }
      if (enabled) {
        params.append('enabled', enabled);
      }
      if (conversation && conversation !== 'all') {
        params.append('conversation_id', conversation);
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
  };

  // Fetch data when filters change
  useEffect(() => {
    fetchAutomations(currentType, currentEnabled, currentConversation);
  }, [currentType, currentEnabled, currentConversation]);

  const handleFiltersSubmit = (e) => {
    e.preventDefault();

    updateSearchParams(filters.type, filters.enabled, filters.conversation);
  };

  const clearFilters = () => {
    setFilters({ type: 'all', enabled: '', conversation: 'all' });
    setSearchParams({});
  };

  const toggleEnabled = async (automationType, automationId, currentEnabled) => {
    try {
      const response = await fetch(`/api/automations/${automationType}/${automationId}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ enabled: !currentEnabled }),
      });

      if (!response.ok) {
        throw new Error('Failed to update automation');
      }

      // Refresh the list
      fetchAutomations(currentType, currentEnabled, currentConversation);
    } catch (err) {
      setError(err.message);
    }
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Never';
    }
    return new Date(timestamp).toLocaleString();
  };

  const formatSourceId = (sourceId) => {
    if (!sourceId) {
      return '';
    }
    return sourceId.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  };

  const formatRecurrenceRule = (rule) => {
    if (!rule) {
      return '';
    }
    return rule;
  };

  const hasAutomations = automations.length > 0;
  const filtersActive =
    currentType !== 'all' || currentEnabled !== '' || currentConversation !== 'all';

  // Extract unique conversation IDs from automations
  const uniqueConversations = Array.from(new Set(automations.map((a) => a.conversation_id))).sort();

  useEffect(() => {
    if (filtersActive) {
      setIsFiltersOpen(true);
    }
  }, [filtersActive]);

  const hero = (
    <section className={styles.hero}>
      <div className={styles.heroHeading}>
        <h1 className={styles.heroTitle}>Automations</h1>
        <p className={styles.heroDescription}>
          Offload recurring work by letting Family Assistant run workflows based on events or
          schedules.
        </p>
      </div>
      {!loading && (
        <span className={styles.countPill}>
          Found {automations.length} automation{automations.length !== 1 ? 's' : ''}
        </span>
      )}
    </section>
  );

  const renderIconBadge = (meta) => {
    const Icon = meta.icon;
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={styles.iconBadge} aria-label={meta.label}>
            <Icon size={18} aria-hidden="true" />
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{meta.label}</TooltipContent>
      </Tooltip>
    );
  };

  const shouldShowList = !loading && hasAutomations;
  const shouldShowEmpty = !loading && !error && !hasAutomations;

  return (
    <TooltipProvider>
      <div className={styles.automationsPage}>
        {hero}

        {error ? (
          <Alert variant="destructive" className={styles.error}>
            <AlertDescription>Error: {error}</AlertDescription>
          </Alert>
        ) : null}

        <section className={styles.ctaGrid} aria-label="Automation quick starts">
          {CREATION_OPTIONS.map((option) => {
            const Icon = option.icon;
            return (
              <Card key={option.id} className={styles.ctaCard}>
                <CardHeader className={styles.ctaHeader}>
                  <div className={styles.ctaIcon} aria-hidden="true">
                    <Icon size={22} />
                  </div>
                  <h2 className={styles.ctaTitle}>{option.title}</h2>
                  <p className={styles.ctaDescription}>{option.description}</p>
                </CardHeader>
                <CardFooter className={styles.ctaFooter}>
                  <Button asChild>
                    <Link to={option.to}>{option.ctaLabel}</Link>
                  </Button>
                </CardFooter>
              </Card>
            );
          })}
        </section>

        <section className={styles.filtersSection}>
          <form onSubmit={handleFiltersSubmit}>
            <details
              className={styles.filtersDetails}
              open={isFiltersOpen}
              onToggle={(event) => setIsFiltersOpen(event.target.open)}
            >
              <summary>
                <Filter size={16} aria-hidden="true" />
                Filters
              </summary>

              <div className={styles.filtersBody}>
                <div className={styles.filtersRow}>
                  <div className={styles.fieldGroup}>
                    <label htmlFor="type" className={styles.fieldLabel}>
                      Automation Type
                    </label>
                    <select
                      name="type"
                      id="type"
                      value={filters.type}
                      onChange={(e) => {
                        const nextType = e.target.value;
                        setFilters((prev) => ({ ...prev, type: nextType }));
                        updateSearchParams(nextType, filters.enabled, filters.conversation);
                      }}
                    >
                      <option value="all">All Types</option>
                      <option value="event">Event-Based</option>
                      <option value="schedule">Schedule-Based</option>
                    </select>
                  </div>

                  <div className={styles.fieldGroup}>
                    <label htmlFor="enabled" className={styles.fieldLabel}>
                      Status
                    </label>
                    <select
                      name="enabled"
                      id="enabled"
                      value={filters.enabled}
                      onChange={(e) => {
                        const nextEnabled = e.target.value;
                        setFilters((prev) => ({ ...prev, enabled: nextEnabled }));
                        updateSearchParams(filters.type, nextEnabled, filters.conversation);
                      }}
                    >
                      <option value="">All</option>
                      <option value="true">Enabled Only</option>
                      <option value="false">Disabled Only</option>
                    </select>
                  </div>

                  {uniqueConversations.length > 0 && (
                    <div className={styles.fieldGroup}>
                      <label htmlFor="conversation" className={styles.fieldLabel}>
                        Conversation
                      </label>
                      <select
                        name="conversation"
                        id="conversation"
                        value={filters.conversation}
                        onChange={(e) => {
                          const nextConversation = e.target.value;
                          setFilters((prev) => ({ ...prev, conversation: nextConversation }));
                          updateSearchParams(filters.type, filters.enabled, nextConversation);
                        }}
                      >
                        <option value="all">All Conversations</option>
                        {uniqueConversations.map((conv) => (
                          <option key={conv} value={conv}>
                            {conv}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>

                <div className={styles.filtersActions}>
                  <Button type="submit">Apply Filters</Button>
                  <Button type="button" variant="secondary" onClick={clearFilters}>
                    Clear Filters
                  </Button>
                </div>
              </div>
            </details>
          </form>
        </section>

        {loading ? (
          <div className={styles.loading}>
            <Loader2 className="animate-spin" aria-hidden="true" />
            <span>Loading automations...</span>
          </div>
        ) : null}

        {shouldShowList ? (
          <div className={styles.automationsGrid}>
            {automations.map((automation) => {
              const typeMeta = TYPE_METADATA[automation.type] || {
                label: 'Automation',
                icon: Zap,
              };
              const actionMeta = ACTION_METADATA[automation.action_type] || {
                label: 'Script Execution',
                icon: ScrollText,
              };
              const statusClassName = `${styles.statusBadge} ${
                automation.enabled ? styles.enabled : styles.disabled
              }`;

              return (
                <article
                  key={automation.id}
                  className={styles.automationCard}
                  data-testid="automation-card"
                  data-automation-name={automation.name}
                >
                  <div className={styles.automationHeader}>
                    <div className={styles.automationTitle}>
                      <h3>
                        <Link to={`/automations/${automation.type}/${automation.id}`}>
                          {automation.name}
                        </Link>
                      </h3>
                      {automation.description ? (
                        <p className={styles.automationDescription}>{automation.description}</p>
                      ) : null}
                    </div>
                    <div className={styles.iconStack}>
                      {renderIconBadge(typeMeta)}
                      {renderIconBadge(actionMeta)}
                    </div>
                  </div>

                  <div className={styles.metaGrid}>
                    <div className={styles.metaItem}>
                      <span className={styles.metaLabel}>Type</span>
                      <span className={styles.metaValue}>{typeMeta.label}</span>
                    </div>

                    <div className={styles.metaItem}>
                      <span className={styles.metaLabel}>Conversation</span>
                      <span className={styles.metaValue}>{automation.conversation_id}</span>
                    </div>

                    {automation.type === 'event' && automation.source_id ? (
                      <div className={styles.metaItem}>
                        <span className={styles.metaLabel}>Source</span>
                        <span className={styles.metaValue}>
                          {formatSourceId(automation.source_id)}
                        </span>
                      </div>
                    ) : null}

                    {automation.type === 'schedule' && automation.recurrence_rule ? (
                      <div className={styles.metaItem}>
                        <span className={styles.metaLabel}>Schedule</span>
                        <span className={styles.metaValue}>
                          {formatRecurrenceRule(automation.recurrence_rule)}
                        </span>
                      </div>
                    ) : null}

                    <div className={styles.metaItem}>
                      <span className={styles.metaLabel}>Status</span>
                      <div className={styles.statusRow}>
                        <span className={statusClassName}>
                          {automation.enabled ? 'Enabled' : 'Disabled'}
                        </span>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            toggleEnabled(automation.type, automation.id, automation.enabled)
                          }
                        >
                          {automation.enabled ? 'Disable' : 'Enable'}
                        </Button>
                      </div>
                    </div>

                    <div className={styles.metaItem}>
                      <span className={styles.metaLabel}>Executions</span>
                      <span className={styles.metaValue}>
                        {automation.execution_count || 0} total
                        {automation.type === 'schedule' && automation.next_scheduled_at ? (
                          <span> (next: {formatTimestamp(automation.next_scheduled_at)})</span>
                        ) : null}
                      </span>
                    </div>
                  </div>

                  <div className={styles.automationFooter}>
                    <div className={styles.timestampGroup}>
                      <span>
                        <strong>Last executed:</strong>{' '}
                        {formatTimestamp(automation.last_execution_at)}
                      </span>
                      <span>
                        <strong>Created:</strong> {formatTimestamp(automation.created_at)}
                      </span>
                    </div>
                    <Link
                      to={`/automations/${automation.type}/${automation.id}`}
                      className={styles.viewLink}
                    >
                      View Details
                      <ArrowRight size={16} aria-hidden="true" />
                    </Link>
                  </div>
                </article>
              );
            })}
          </div>
        ) : null}

        {shouldShowEmpty ? (
          <div className={styles.emptyState}>
            <h2 className={styles.emptyTitle}>You haven&apos;t created any automations yet</h2>
            <p className={styles.emptyDescription}>
              Automations help Family Assistant follow through on your routines automatically. Start
              with an event or schedule to see tasks happen hands-free.
            </p>
            <div className={styles.emptyActions}>
              <Button asChild>
                <Link to="/automations/create/event">Create your first automation</Link>
              </Button>
              <Button asChild variant="secondary">
                <Link to="/docs/">Explore documentation</Link>
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </TooltipProvider>
  );
};

export default AutomationsList;
