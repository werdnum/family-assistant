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
          <NavigationMenu className="mx-auto max-w-full">
            <NavigationMenuList className="flex-nowrap justify-start gap-2 px-4 py-3 overflow-x-auto">
              {/* Assistant Data */}
              <NavigationMenuItem>
                <NavigationMenuTrigger className="text-sm">
                  <FileText className="mr-2 h-4 w-4" />
                  Data
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 w-[200px]">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/notes">
                        <FileText className="mr-2 h-4 w-4" />
                        Notes
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <NavLink to="/context" isActive={currentPage === 'context'}>
                        <FileText className="mr-2 h-4 w-4" />
                        Context
                      </NavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Documents */}
              <NavigationMenuItem>
                <NavigationMenuTrigger className="text-sm">
                  <FolderOpen className="mr-2 h-4 w-4" />
                  Documents
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 w-[200px]">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/documents/">
                        <FolderOpen className="mr-2 h-4 w-4" />
                        List
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/documents/upload">
                        <Upload className="mr-2 h-4 w-4" />
                        Upload
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/vector-search">
                        <Search className="mr-2 h-4 w-4" />
                        Search
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
                    className="whitespace-nowrap"
                  >
                    <MessageCircle className="mr-2 h-4 w-4" />
                    Chat
                  </NavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <NavigationMenuItem>
                <NavigationMenuLink asChild>
                  <ExternalNavLink href="/history">
                    <History className="mr-2 h-4 w-4" />
                    History
                  </ExternalNavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Automation */}
              <NavigationMenuItem>
                <NavigationMenuTrigger className="text-sm">
                  <Zap className="mr-2 h-4 w-4" />
                  Automation
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 w-[200px]">
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/events">
                        <Calendar className="mr-2 h-4 w-4" />
                        Events
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/event-listeners">
                        <Settings className="mr-2 h-4 w-4" />
                        Event Listeners
                      </ExternalNavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Internal/Admin */}
              <NavigationMenuItem>
                <NavigationMenuTrigger className="text-sm">
                  <Cog className="mr-2 h-4 w-4" />
                  Internal
                </NavigationMenuTrigger>
                <NavigationMenuContent>
                  <div className="grid gap-3 p-4 w-[200px]">
                    <NavigationMenuLink asChild>
                      <NavLink to="/tools" isActive={currentPage === 'tools'}>
                        <Cog className="mr-2 h-4 w-4" />
                        Tools
                      </NavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <ExternalNavLink href="/tasks">
                        <Settings className="mr-2 h-4 w-4" />
                        Task Queue
                      </ExternalNavLink>
                    </NavigationMenuLink>
                    <NavigationMenuLink asChild>
                      <NavLink to="/errors" isActive={currentPage === 'errors'}>
                        <AlertTriangle className="mr-2 h-4 w-4" />
                        Error Logs
                      </NavLink>
                    </NavigationMenuLink>
                  </div>
                </NavigationMenuContent>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Help */}
              <NavigationMenuItem>
                <NavigationMenuLink asChild>
                  <ExternalNavLink href="/docs/">
                    <HelpCircle className="mr-2 h-4 w-4" />
                    Help
                  </ExternalNavLink>
                </NavigationMenuLink>
              </NavigationMenuItem>

              <Separator orientation="vertical" className="h-6" />

              {/* Theme Toggle */}
              <NavigationMenuItem>
                <ThemeToggle />
              </NavigationMenuItem>
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
