import React, { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '@/components/ui/button';

export const TooltipIconButton = React.forwardRef(
  (
    { tooltip, variant = 'ghost', size = 'md', side: _side = 'top', className, children, ...props },
    ref
  ) => {
    const [showTooltip, setShowTooltip] = useState(false);
    const [position, setPosition] = useState({ top: 0, left: 0 });
    const tooltipId = useId();
    const buttonRef = useRef(null);
    // Icon-only buttons have no text content, so a string tooltip doubles as
    // the accessible name unless the caller supplies one. When it is the name,
    // pointing aria-describedby at it as well would announce it twice.
    const ariaLabel = props['aria-label'] ?? (typeof tooltip === 'string' ? tooltip : undefined);
    const describedBy = tooltip && ariaLabel !== tooltip ? tooltipId : undefined;

    useEffect(() => {
      if (showTooltip && buttonRef.current) {
        const rect = buttonRef.current.getBoundingClientRect();
        const scrollX = window.scrollX || window.pageXOffset;
        const scrollY = window.scrollY || window.pageYOffset;

        // Position tooltip above button by default
        setPosition({
          top: rect.top + scrollY - 40, // 40px above button
          left: rect.left + scrollX + rect.width / 2, // centered horizontally
        });
      }
    }, [showTooltip]);

    return (
      <>
        <Button
          ref={(node) => {
            buttonRef.current = node;
            if (typeof ref === 'function') {
              ref(node);
            } else if (ref) {
              ref.current = node;
            }
          }}
          variant={variant}
          size={size}
          className={className}
          onMouseEnter={() => setShowTooltip(true)}
          onMouseLeave={() => setShowTooltip(false)}
          onFocus={() => setShowTooltip(true)}
          onBlur={() => setShowTooltip(false)}
          aria-describedby={describedBy}
          {...props}
          aria-label={ariaLabel}
        >
          {children}
        </Button>
        {showTooltip &&
          tooltip &&
          createPortal(
            <div
              id={tooltipId}
              role="tooltip"
              className="tooltip"
              style={{
                position: 'absolute',
                top: `${position.top}px`,
                left: `${position.left}px`,
                transform: 'translateX(-50%)',
                zIndex: 50,
                pointerEvents: 'none',
                backgroundColor: 'hsl(var(--popover))',
                color: 'hsl(var(--popover-foreground))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '0.375rem',
                padding: '0.375rem 0.75rem',
                fontSize: '0.875rem',
                boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)',
                whiteSpace: 'nowrap',
              }}
            >
              {tooltip}
            </div>,
            document.body
          )}
      </>
    );
  }
);
