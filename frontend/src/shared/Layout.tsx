import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  Menu,
  FileText,
  Search,
  MessageCircle,
  History,
  Calendar,
  Cog,
  HelpCircle,
  FolderOpen,
  Upload,
  Settings,
  Zap,
  AlertTriangle,
} from 'lucide-react';

import {
  NavigationMenu,
  NavigationMenuContent,
  NavigationMenuItem,
  NavigationMenuLink,
  NavigationMenuList,
  NavigationMenuTrigger,
  NavigationMenuIndicator,
  navigationMenuTriggerStyle,
} from '@/components/ui/navigation-menu';
import NavigationSheet from './NavigationSheet';
import { ThemeToggle } from './ThemeToggle';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface LayoutProps {
  children: React.ReactNode;
}

const Layout: React.FC<LayoutProps> = ({ children }) => {
  const location = useLocation();

  // Extract current page from pathname
  const currentPage = location.pathname.split('/')[1] || 'home';

  // Function to handle NavigationMenu dropdown positioning
  function onNavChange(value: string) {
    // Use the menu value to find the specific trigger
    setTimeout(() => {
      // Find the specific trigger that was just opened using the menu value
      const triggers = document.querySelectorAll('.submenu-trigger[data-state="open"]');
      const viewports = document.querySelectorAll('.nav-viewport[data-state="open"]');

      // Check if both triggers and viewports are present
      if (!triggers.length || !viewports.length) {
        return;
      }

      // Find the trigger that matches the current menu value
      let trigger: HTMLElement | null = null;
      for (const t of triggers) {
        const menuItem = t.closest('[data-value]');
        const menuValue = menuItem?.getAttribute('data-value');
        if (menuItem && menuValue === value) {
          trigger = t as HTMLElement;
          break;
        }
      }

      // Fallback to last trigger if specific one not found
      if (!trigger) {
        trigger = triggers[triggers.length - 1] as HTMLElement;
      }

      const viewport = viewports[viewports.length - 1] as HTMLElement;

      // Wait a bit for the viewport to fully render and get its dimensions
      requestAnimationFrame(() => {
        const { offsetLeft, offsetWidth } = trigger;
        const menuWidth = viewport.offsetWidth || 200;

        // Calculate position to center under trigger
        let menuLeftPosition = offsetLeft + offsetWidth / 2 - menuWidth / 2;

        // Prevent overflow on the left side
        if (menuLeftPosition < 0) {
          menuLeftPosition = 0;
        }

        // Prevent overflow on the right side
        const windowWidth = window.innerWidth;
        if (menuLeftPosition + menuWidth > windowWidth) {
          menuLeftPosition = windowWidth - menuWidth - 16; // 16px margin
        }

        // Apply the calculated position
        document.documentElement.style.setProperty('--menu-left-position', `${menuLeftPosition}px`);
      });
    }, 10);
  }

  const NavLink = React.forwardRef<
    React.ElementRef<typeof Link>,
    React.ComponentPropsWithoutRef<typeof Link> & {
      className?: string;
      isActive?: boolean;
    }
  >(({ className, isActive, ...props }, ref) => {
    return (
      <Link
        ref={ref}
        className={cn(
          navigationMenuTriggerStyle(),
          isActive && 'bg-accent/50 text-accent-foreground',
          className
        )}
        {...props}
      />
    );
  });
  NavLink.displayName = 'NavLink';

  const ExternalNavLink = React.forwardRef<
    HTMLAnchorElement,
    React.AnchorHTMLAttributes<HTMLAnchorElement> & {
      className?: string;
    }
  >(({ className, ...props }, ref) => {
    return <a ref={ref} className={cn(navigationMenuTriggerStyle(), className)} {...props} />;
  });
  ExternalNavLink.displayName = 'ExternalNavLink';

  return (
    <>
      <header className="border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        {/* Desktop Navigation */}
        <div className="hidden md:block">
          <NavigationMenu className="max-w-full" onValueChange={onNavChange}>
            <NavigationMenuList className="flex-nowrap justify-start gap-1 px-4 py-3 overflow-x-auto">
              {/* Assistant Data */}
              <NavigationMenuItem value="data">
                <NavigationMenuTrigger className="submenu-trigger text-sm whitespace-nowrap">
                  <FileText className="mr-2 h-4 w-4 flex-shrink-0" />
                  <span className="whitespace-nowrap">Data</span>
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 min-w-[200px] w-max">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/notes" className="whitespace-nowrap">
                        <FileText className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Notes</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <NavLink
                        to="/context"
                        isActive={currentPage === 'context'}
                        className="whitespace-nowrap"
                      >
                        <FileText className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Context</span>
                      </NavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Documents */}
              <NavigationMenuItem value="documents">
                <NavigationMenuTrigger className="submenu-trigger text-sm whitespace-nowrap">
                  <FolderOpen className="mr-2 h-4 w-4 flex-shrink-0" />
                  <span className="whitespace-nowrap">Documents</span>
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 min-w-[200px] w-max">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/documents/" className="whitespace-nowrap">
                        <FolderOpen className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>List</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/documents/upload" className="whitespace-nowrap">
                        <Upload className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Upload</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/vector-search" className="whitespace-nowrap">
                        <Search className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Search</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Chat & History */}
              <NavigationMenuItem>
                <NavigationMenuLink asChild>
                  <NavLink
                    to="/chat"
                    isActive={currentPage === 'chat'}
                    className="whitespace-nowrap inline-flex items-center"
                  >
                    <MessageCircle className="mr-2 h-4 w-4 flex-shrink-0" />
                    <span className="whitespace-nowrap">Chat</span>
                  </NavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <NavigationMenuItem>
                <NavigationMenuLink asChild>
                  <ExternalNavLink href="/history" className="inline-flex items-center">
                    <History className="mr-2 h-4 w-4 flex-shrink-0" />
                    <span className="whitespace-nowrap">History</span>
                  </ExternalNavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Automation */}
              <NavigationMenuItem value="automation">
                <NavigationMenuTrigger className="submenu-trigger text-sm whitespace-nowrap">
                  <Zap className="mr-2 h-4 w-4 flex-shrink-0" />
                  <span className="whitespace-nowrap">Automation</span>
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 min-w-[200px] w-max">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/events" className="whitespace-nowrap">
                        <Calendar className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Events</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/event-listeners" className="whitespace-nowrap">
                        <Settings className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Event Listeners</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Internal/Admin */}
              <NavigationMenuItem value="internal">
                <NavigationMenuTrigger className="submenu-trigger text-sm whitespace-nowrap">
                  <Cog className="mr-2 h-4 w-4 flex-shrink-0" />
                  <span className="whitespace-nowrap">Internal</span>
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 min-w-[200px] w-max">
                    <NavigationMenuLink asChild>
                      <NavLink
                        to="/tools"
                        isActive={currentPage === 'tools'}
                        className="whitespace-nowrap"
                      >
                        <Cog className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Tools</span>
                      </NavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/tasks" className="whitespace-nowrap">
                        <Settings className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Task Queue</span>
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <NavLink
                        to="/errors"
                        isActive={currentPage === 'errors'}
                        className="whitespace-nowrap"
                      >
                        <AlertTriangle className="mr-2 h-4 w-4 flex-shrink-0" />
                        <span>Error Logs</span>
                      </NavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Help */}
              <NavigationMenuItem>
                <NavigationMenuLink asChild>
                  <ExternalNavLink href="/docs/" className="inline-flex items-center">
                    <HelpCircle className="mr-2 h-4 w-4 flex-shrink-0" />
                    <span className="whitespace-nowrap">Help</span>
                  </ExternalNavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Theme Toggle */}
              <NavigationMenuItem>
                <ThemeToggle />
              </NavigationMenuItem>

              <NavigationMenuIndicator />
            </NavigationMenuList>
          </NavigationMenu>
        </div>

        {/* Mobile Navigation */}
        <div className="md:hidden flex items-center justify-between px-4 py-3">
          <div className="text-lg font-semibold">Family Assistant</div>
          <NavigationSheet
            currentPage={currentPage}
            title="Navigation"
            description="Browse the Family Assistant features"
            side="left"
          >
            <Button variant="outline" size="icon">
              <Menu className="h-4 w-4" />
              <span className="sr-only">Open navigation menu</span>
            </Button>
          </NavigationSheet>
        </div>
      </header>

      <main className="flex-1">{children}</main>

      <footer className="border-t bg-muted/50 py-6 md:py-0">
        <div className="container flex flex-col items-center justify-between gap-4 md:h-24 md:flex-row">
          <p className="text-center text-sm leading-loose text-muted-foreground md:text-left">
            &copy; {new Date().getFullYear()} Family Assistant
          </p>
        </div>
      </footer>
    </>
  );
};

export default Layout;
