import React, { useState } from 'react';
import { ChevronDownIcon, WrenchIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';

interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

const ToolGroup: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const toolCount = endIndex - startIndex + 1;
  const [isExpanded, setIsExpanded] = useState(true); // Start expanded so attachments are immediately visible

  return (
    <div className="my-4 border border-border rounded-lg bg-card/50 p-3" data-testid="tool-group">
      <Collapsible open={isExpanded} onOpenChange={setIsExpanded}>
        <CollapsibleTrigger
          className={cn(
            'flex w-full items-center justify-between gap-2 py-2 px-1 text-sm font-medium',
            'text-muted-foreground hover:text-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded-sm'
          )}
          data-testid="tool-group-trigger"
        >
          <div className="flex items-center gap-2">
            <WrenchIcon className="h-4 w-4" />
            <span>
              {toolCount} tool {toolCount === 1 ? 'call' : 'calls'}
            </span>
          </div>
          <ChevronDownIcon
            className={cn('h-4 w-4 transition-transform duration-200', isExpanded && 'rotate-180')}
          />
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-2 pt-2" data-testid="tool-group-content">
          {children}
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
};

export { ToolGroup };
