import React, { useState, useId } from 'react';
import { Button } from '@/components/ui/button';

export const TooltipIconButton = React.forwardRef(
  ({ tooltip, variant = 'ghost', size = 'md', className, children, ...props }, ref) => {
    const [showTooltip, setShowTooltip] = useState(false);
    const tooltipId = useId();

    return (
      <div className="tooltip-wrapper">
        <Button
          ref={ref}
          variant={variant}
          size={size}
          className={className}
          onMouseEnter={() => setShowTooltip(true)}
          onMouseLeave={() => setShowTooltip(false)}
          onFocus={() => setShowTooltip(true)}
          onBlur={() => setShowTooltip(false)}
          aria-describedby={tooltip ? tooltipId : undefined}
          {...props}
        >
          {children}
        </Button>
        {showTooltip && tooltip && (
          <div id={tooltipId} role="tooltip" className="tooltip">
            {tooltip}
          </div>
        )}
      </div>
    );
  }
);
