import React, { useState, useId } from 'react';
import classNames from 'classnames';

export const TooltipIconButton = React.forwardRef(({ 
  tooltip, 
  variant = 'ghost',
  size = 'md',
  className,
  children,
  ...props 
}, ref) => {
  const [showTooltip, setShowTooltip] = useState(false);
  const tooltipId = useId();
  
  return (
    <div className="tooltip-wrapper">
      <button
        ref={ref}
        className={classNames(
          'icon-button',
          `icon-button-${variant}`,
          `icon-button-${size}`,
          className
        )}
        onMouseEnter={() => setShowTooltip(true)}
        onMouseLeave={() => setShowTooltip(false)}
        onFocus={() => setShowTooltip(true)}
        onBlur={() => setShowTooltip(false)}
        aria-describedby={tooltip ? tooltipId : undefined}
        {...props}
      >
        {children}
      </button>
      {showTooltip && tooltip && (
        <div 
          id={tooltipId}
          role="tooltip" 
          className="tooltip"
        >
          {tooltip}
        </div>
      )}
    </div>
  );
});
