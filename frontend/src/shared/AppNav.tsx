import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { isNavItemActive, NAV_FOOTER_ITEMS, NAV_SECTIONS, type NavItem } from './navigation';

interface NavLinkProps {
  item: NavItem;
  collapsed: boolean;
  onNavigate?: () => void;
}

const NavLink: React.FC<NavLinkProps> = ({ item, collapsed, onNavigate }) => {
  const { pathname } = useLocation();
  const active = isNavItemActive(item, pathname);
  const Icon = item.icon;

  const link = (
    <Link
      to={item.to}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      aria-label={collapsed ? item.title : undefined}
      className={cn(
        'group flex h-8 items-center gap-3 rounded-md px-2.5 text-sm font-medium text-muted-foreground transition-colors',
        'hover:bg-accent hover:text-accent-foreground',
        'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
        active && 'bg-accent text-foreground',
        collapsed && 'justify-center px-0'
      )}
    >
      <Icon
        className={cn(
          'h-4 w-4 shrink-0',
          active ? 'text-primary' : 'text-muted-foreground group-hover:text-accent-foreground'
        )}
        aria-hidden="true"
      />
      {!collapsed && <span className="truncate">{item.title}</span>}
    </Link>
  );

  if (!collapsed) {
    return link;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.title}</TooltipContent>
    </Tooltip>
  );
};

interface AppNavProps {
  /** Icon-only rail: section titles hidden, labels moved into tooltips. */
  collapsed?: boolean;
  /** Called after a link is chosen, e.g. to close the mobile drawer. */
  onNavigate?: () => void;
  className?: string;
}

/** The main section links, grouped. Shared by the desktop sidebar and mobile drawer. */
export const AppNav: React.FC<AppNavProps> = ({ collapsed = false, onNavigate, className }) => (
  <nav aria-label="Main" className={cn('flex flex-col gap-3', className)}>
    {NAV_SECTIONS.map((section, index) => (
      <div key={section.title ?? index} className="flex flex-col gap-0.5">
        {section.title &&
          (collapsed ? (
            <div className="mx-auto mb-1 h-px w-6 bg-border" aria-hidden="true" />
          ) : (
            <h2 className="mb-1 px-2.5 text-xs font-medium uppercase tracking-wider text-muted-foreground/70">
              {section.title}
            </h2>
          ))}
        <ul className="flex flex-col gap-0.5">
          {section.items.map((item) => (
            <li key={item.to}>
              <NavLink item={item} collapsed={collapsed} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>
      </div>
    ))}
  </nav>
);

/** Help and About, rendered at the foot of the sidebar and drawer. */
export const AppNavFooter: React.FC<AppNavProps> = ({
  collapsed = false,
  onNavigate,
  className,
}) => (
  <ul aria-label="Help and information" className={cn('flex flex-col gap-0.5', className)}>
    {NAV_FOOTER_ITEMS.map((item) => (
      <li key={item.to}>
        <NavLink item={item} collapsed={collapsed} onNavigate={onNavigate} />
      </li>
    ))}
  </ul>
);
