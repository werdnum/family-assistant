import { ChevronsLeft, ChevronsRight, Menu } from 'lucide-react';
import React, { useCallback, useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import logoUrl from '@/assets/logo-64.png';
import { AppNav, AppNavFooter } from './AppNav';
import { findActiveNavItem } from './navigation';
import { ThemeToggle } from './ThemeToggle';

const COLLAPSED_STORAGE_KEY = 'family-assistant-nav-collapsed';
// Between the md and lg breakpoints a full-width sidebar would take a third of
// the screen, so it is always shown as the icon rail there.
const WIDE_QUERY = '(min-width: 1024px)';

function readCollapsedPreference(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_STORAGE_KEY) === 'true';
  } catch {
    // Storage can be unavailable (private mode, blocked site data); use the default.
    return false;
  }
}

function useIsWide(): boolean {
  const [isWide, setIsWide] = useState(() => window.matchMedia(WIDE_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(WIDE_QUERY);
    const update = () => setIsWide(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return isWide;
}

function useCollapsedPreference(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(readCollapsedPreference);
  const toggle = useCallback(() => {
    setCollapsed((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(COLLAPSED_STORAGE_KEY, String(next));
      } catch {
        // The preference simply won't survive a reload.
      }
      return next;
    });
  }, []);
  return [collapsed, toggle];
}

interface BrandProps {
  showName: boolean;
  onNavigate?: () => void;
}

const Brand: React.FC<BrandProps> = ({ showName, onNavigate }) => (
  <Link
    to="/"
    onClick={onNavigate}
    aria-label={showName ? undefined : 'Family Assistant home'}
    className="flex min-w-0 items-center gap-2.5 rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
  >
    <img src={logoUrl} alt="" className="h-7 w-7 shrink-0 rounded-md" />
    {showName && <span className="truncate text-sm font-semibold">Family Assistant</span>}
  </Link>
);

/** The hamburger button and the navigation drawer it opens, for narrow screens. */
export const MobileNav: React.FC = () => {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="h-9 w-9 shrink-0">
          <Menu className="h-5 w-5" />
          <span className="sr-only">Open navigation menu</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="flex w-72 flex-col gap-0 p-0 sm:max-w-xs">
        <SheetTitle className="sr-only">Navigation</SheetTitle>
        <SheetDescription className="sr-only">
          Go to another part of Family Assistant
        </SheetDescription>
        <div className="flex h-14 shrink-0 items-center border-b px-4">
          <Brand showName onNavigate={close} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
          <AppNav onNavigate={close} />
        </div>
        <div className="flex shrink-0 flex-col gap-1 border-t px-3 py-3">
          <AppNavFooter onNavigate={close} />
          <div className="flex items-center justify-between px-2.5 pt-1">
            <span className="text-sm text-muted-foreground">Theme</span>
            <ThemeToggle variant="ghost" size="icon" />
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
};

interface AppTopBarProps {
  /** Shown beside the menu button; defaults to the current page's navigation title. */
  title?: React.ReactNode;
  /** Page-specific buttons on the right. */
  actions?: React.ReactNode;
  className?: string;
}

/**
 * The narrow-screen header: menu button, page title, and page actions. The shell
 * renders it for ordinary pages; full-bleed pages such as chat render it
 * themselves so they can supply their own actions.
 */
export const AppTopBar: React.FC<AppTopBarProps> = ({ title, actions, className }) => {
  const { pathname } = useLocation();
  const resolvedTitle = title ?? findActiveNavItem(pathname)?.title ?? 'Family Assistant';

  return (
    <header
      className={cn(
        'sticky top-0 z-40 flex h-14 shrink-0 items-center gap-1 border-b bg-background/95 px-2 backdrop-blur supports-[backdrop-filter]:bg-background/60',
        className
      )}
    >
      <MobileNav />
      <div className="min-w-0 flex-1 truncate px-1 text-base font-semibold">{resolvedTitle}</div>
      {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
    </header>
  );
};

interface SidebarProps {
  collapsed: boolean;
  /** Absent where the rail is the only layout that fits. */
  onToggleCollapsed?: () => void;
}

const Sidebar: React.FC<SidebarProps> = ({ collapsed, onToggleCollapsed }) => {
  const toggleLabel = collapsed ? 'Expand navigation' : 'Collapse navigation';
  const ToggleIcon = collapsed ? ChevronsRight : ChevronsLeft;

  return (
    <aside
      data-testid="app-sidebar"
      data-collapsed={collapsed}
      className={cn(
        'sticky top-0 hidden h-dvh shrink-0 flex-col border-r bg-muted/30 transition-[width] duration-200 md:flex',
        collapsed ? 'w-14' : 'w-60'
      )}
    >
      <div
        className={cn(
          'flex h-14 shrink-0 items-center',
          collapsed ? 'justify-center px-2' : 'justify-between gap-2 pl-4 pr-2'
        )}
      >
        {onToggleCollapsed === undefined ? (
          <Brand showName={false} />
        ) : (
          <>
            {!collapsed && <Brand showName />}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 shrink-0 text-muted-foreground"
                  onClick={onToggleCollapsed}
                  aria-label={toggleLabel}
                  aria-expanded={!collapsed}
                >
                  <ToggleIcon className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="right">{toggleLabel}</TooltipContent>
            </Tooltip>
          </>
        )}
      </div>
      <div className={cn('min-h-0 flex-1 overflow-y-auto py-2', collapsed ? 'px-2' : 'px-3')}>
        <AppNav collapsed={collapsed} />
      </div>
      <div
        className={cn(
          'flex shrink-0 flex-col gap-1 border-t py-3',
          collapsed ? 'items-stretch px-2' : 'px-3'
        )}
      >
        <AppNavFooter collapsed={collapsed} />
        <div
          className={cn(
            'flex items-center',
            collapsed ? 'justify-center' : 'justify-between pl-2.5'
          )}
        >
          {!collapsed && <span className="text-sm text-muted-foreground">Theme</span>}
          <ThemeToggle variant="ghost" size="icon" />
        </div>
      </div>
    </aside>
  );
};

interface AppShellProps {
  children: React.ReactNode;
  /**
   * The page fills the viewport and manages its own scrolling and narrow-screen
   * header (rendering `AppTopBar` itself), as chat does.
   */
  fullBleed?: boolean;
}

/**
 * The frame around every page: a collapsible sidebar on wide screens, and a top
 * bar with a navigation drawer on narrow ones.
 */
const AppShell: React.FC<AppShellProps> = ({ children, fullBleed = false }) => {
  const [collapsed, toggleCollapsed] = useCollapsedPreference();
  const isWide = useIsWide();

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex min-h-dvh bg-background">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-background focus:p-3 focus:ring-2 focus:ring-ring"
        >
          Skip to content
        </a>
        <Sidebar
          collapsed={collapsed || !isWide}
          onToggleCollapsed={isWide ? toggleCollapsed : undefined}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          {!fullBleed && <AppTopBar className="md:hidden" />}
          <main
            id="main-content"
            tabIndex={-1}
            className={cn('min-w-0 flex-1 focus:outline-hidden', fullBleed && 'flex flex-col')}
          >
            {children}
          </main>
        </div>
      </div>
    </TooltipProvider>
  );
};

export default AppShell;
