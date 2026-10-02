import type * as React from 'react';

import { cn } from '@/lib/utils';

interface PageHeaderProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Buttons or links aligned to the right of the title on wide screens. */
  actions?: React.ReactNode;
  /** Rendered above the title, e.g. a back link. */
  eyebrow?: React.ReactNode;
  className?: string;
}

const PageHeader = ({ title, description, actions, eyebrow, className }: PageHeaderProps) => (
  <header className={cn('mb-6 space-y-2', className)}>
    {eyebrow}
    <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0 space-y-1">
        <h1 className="break-words text-3xl font-bold tracking-tight">{title}</h1>
        {description ? <p className="max-w-2xl text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2 sm:shrink-0">{actions}</div> : null}
    </div>
  </header>
);

interface PageContainerProps {
  children: React.ReactNode;
  className?: string;
}

/** Standard width and padding for pages rendered inside the main Layout. */
const PageContainer = ({ children, className }: PageContainerProps) => (
  <div className={cn('container mx-auto px-4 py-6 sm:px-8', className)}>{children}</div>
);

export { PageContainer, PageHeader };
