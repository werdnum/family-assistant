import { useAuiState } from '@assistant-ui/react';
import { ChevronDownIcon } from 'lucide-react';
import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import { ToolConfirmationContext } from './ToolConfirmationContext';
import {
  categoryInfo,
  generateToolGroupSummary,
  getToolIconInfo,
  type ToolCategory,
} from './toolIconMapping';

interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

interface ToolGroupState {
  toolNames: string[];
  toolCallIds: string[];
  hasUnfinishedTool: boolean;
}

const DEFAULT_TOOL_GROUP_STATE: ToolGroupState = {
  toolNames: [],
  toolCallIds: [],
  hasUnfinishedTool: false,
};

// Hook to safely access message state with fallback
function useSafeToolGroupState(startIndex: number, endIndex: number): ToolGroupState {
  try {
    const serializedState = useAuiState((s) => {
      const parts = s.message.parts;
      const toolNames: string[] = [];
      const toolCallIds: string[] = [];
      let hasUnfinishedTool = false;

      for (let i = startIndex; i <= endIndex && i < parts.length; i++) {
        const part = parts[i];
        if (part.type === 'tool-call') {
          toolNames.push(part.toolName);
          toolCallIds.push(part.toolCallId);

          if (part.status.type !== 'complete') {
            hasUnfinishedTool = true;
          }
        }
      }

      return JSON.stringify({ toolNames, toolCallIds, hasUnfinishedTool });
    });

    return useMemo(() => JSON.parse(serializedState) as ToolGroupState, [serializedState]);
  } catch {
    // Fallback when message context is not available (e.g., in tests)
    return DEFAULT_TOOL_GROUP_STATE;
  }
}

const ToolGroup: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const context = useContext(ToolConfirmationContext);

  const { toolNames, toolCallIds, hasUnfinishedTool } = useSafeToolGroupState(startIndex, endIndex);
  const hasPendingConfirmation = toolCallIds.some((toolCallId) =>
    context?.pendingConfirmations?.has(toolCallId)
  );
  const shouldAutoExpand = hasUnfinishedTool || hasPendingConfirmation;
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);
  const hasUserToggled = useRef(false);

  useEffect(() => {
    if (!hasUserToggled.current) {
      setIsExpanded(shouldAutoExpand);
    }
  }, [shouldAutoExpand]);

  const handleOpenChange = (open: boolean) => {
    hasUserToggled.current = true;
    setIsExpanded(open);
  };

  const toolCount = endIndex - startIndex + 1;

  // Generate summary text
  const summaryText = useMemo(() => {
    if (toolNames.length === 0) {
      // Fallback to generic count when tool names unavailable
      return `${toolCount} tool ${toolCount === 1 ? 'call' : 'calls'}`;
    }
    return generateToolGroupSummary(toolNames);
  }, [toolNames, toolCount]);

  // Get icons for the first few unique categories (max 4)
  const categoryIcons = useMemo(() => {
    const icons: Array<{
      Icon: React.ComponentType<{ className?: string }>;
      category: ToolCategory;
    }> = [];
    const seenCategories = new Set<ToolCategory>();

    for (const toolName of toolNames) {
      if (icons.length >= 4) {
        break;
      }

      const iconInfo = getToolIconInfo(toolName);
      if (!seenCategories.has(iconInfo.category)) {
        icons.push({ Icon: iconInfo.icon, category: iconInfo.category });
        seenCategories.add(iconInfo.category);
      }
    }

    return icons;
  }, [toolNames]);

  return (
    <div
      className={cn(
        'my-4 rounded-lg border border-border',
        'bg-gradient-to-br from-card/80 to-card/40',
        'shadow-sm hover:shadow-md transition-shadow duration-200'
      )}
      data-testid="tool-group"
    >
      <Collapsible open={isExpanded} onOpenChange={handleOpenChange}>
        <CollapsibleTrigger
          className={cn(
            'flex w-full items-center justify-between gap-3 py-3 px-4 text-sm font-medium',
            'text-muted-foreground hover:text-foreground',
            'transition-colors duration-150',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'focus-visible:ring-offset-2 rounded-lg'
          )}
          data-testid="tool-group-trigger"
        >
          <div className="flex items-center gap-3 flex-1 min-w-0">
            {/* Category icons */}
            <div className="flex items-center -space-x-1">
              {categoryIcons.map(({ Icon, category }, index) => (
                <div
                  key={category}
                  className={cn(
                    'flex items-center justify-center w-7 h-7 rounded-full',
                    'bg-background border-2 border-border',
                    'transition-transform duration-150',
                    isExpanded ? 'scale-100' : 'scale-90'
                  )}
                  style={{ zIndex: categoryIcons.length - index }}
                >
                  <Icon className={cn('h-3.5 w-3.5', categoryInfo[category].color)} />
                </div>
              ))}
            </div>

            {/* Summary text */}
            <span className="truncate">{summaryText}</span>
          </div>

          {/* Chevron indicator */}
          <ChevronDownIcon className="h-4 w-4 flex-shrink-0 transition-transform duration-200" />
        </CollapsibleTrigger>
        <CollapsibleContent className="px-4 pb-3" data-testid="tool-group-content">
          <div className="space-y-2 pt-2 border-t border-border/50">{children}</div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
};

export { ToolGroup };
