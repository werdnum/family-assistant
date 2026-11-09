import { AlertTriangleIcon, ChevronDownIcon, HandIcon, Loader2Icon } from 'lucide-react';
import React, { useMemo } from 'react';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import { categoryInfo, getToolIconInfo } from './toolIconMapping';
import { summarizeToolGroup, type ToolGroupCall } from './toolGroupSummary';

interface ToolGroupShellProps {
  calls: readonly ToolGroupCall[];
  isExpanded: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}

/**
 * Presentation-only chrome for a group of consecutive tool calls. Kept free of
 * assistant-ui runtime state so the read-only shared transcript can render the
 * same treatment as the live thread.
 *
 * Collapsed, it is one line: what is happening now while the group runs, and
 * the call that mattered most once it has finished.
 */
const ToolGroupShell: React.FC<ToolGroupShellProps> = ({
  calls,
  isExpanded,
  onOpenChange,
  children,
}) => {
  const summary = useMemo(() => summarizeToolGroup(calls), [calls]);
  const hasFailures = calls.some((call) => call.state === 'unsuccessful');
  const leadInfo = summary.subject ? getToolIconInfo(summary.subject.toolName) : null;

  let icon: React.ReactNode;
  if (summary.needsApproval) {
    icon = <HandIcon className="h-4 w-4 text-amber-600 dark:text-amber-400" />;
  } else if (summary.isRunning) {
    icon = <Loader2Icon className="h-4 w-4 animate-spin" data-testid="tool-group-spinner" />;
  } else if (hasFailures) {
    icon = <AlertTriangleIcon className="h-4 w-4 text-destructive" />;
  } else if (leadInfo) {
    const LeadIcon = leadInfo.icon;
    icon = <LeadIcon className={cn('h-4 w-4', categoryInfo[leadInfo.category].color)} />;
  }

  return (
    <div
      className={cn('my-2 rounded-lg border border-border/60 bg-card/40')}
      data-testid="tool-group"
      data-running={summary.isRunning ? 'true' : 'false'}
    >
      <Collapsible open={isExpanded} onOpenChange={onOpenChange}>
        <CollapsibleTrigger
          className={cn(
            'flex w-full items-center gap-2 py-2 px-3 text-sm',
            'text-muted-foreground hover:text-foreground',
            'transition-colors duration-150',
            'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
            'focus-visible:ring-offset-2 rounded-lg'
          )}
          data-testid="tool-group-trigger"
        >
          <span className="flex h-4 w-4 shrink-0 items-center justify-center">{icon}</span>
          <span
            className={cn(
              'min-w-0 flex-1 truncate text-left',
              summary.isRunning && !summary.needsApproval && 'animate-pulse text-foreground'
            )}
            data-testid="tool-group-headline"
          >
            {summary.headline}
          </span>
          {summary.detail && (
            <span
              className={cn('shrink-0 text-xs', hasFailures && 'text-destructive')}
              data-testid="tool-group-detail"
            >
              {summary.detail}
            </span>
          )}
          <ChevronDownIcon
            className={cn(
              'h-4 w-4 flex-shrink-0 transition-transform duration-200',
              isExpanded && 'rotate-180'
            )}
          />
        </CollapsibleTrigger>
        <CollapsibleContent className="px-3 pb-3" data-testid="tool-group-content">
          <div className="space-y-2 pt-2 border-t border-border/50">{children}</div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
};

export { ToolGroupShell };
