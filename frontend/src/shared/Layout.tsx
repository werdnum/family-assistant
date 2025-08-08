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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
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

  const mobileMenuItems = [
    { type: 'section', title: 'Data' },
    { type: 'external', href: '/notes', title: 'Notes', icon: FileText },
    { type: 'link', to: '/context', title: 'Context', icon: FileText },
    { type: 'section', title: 'Documents' },
    { type: 'external', href: '/documents/', title: 'List', icon: FolderOpen },
    { type: 'external', href: '/documents/upload', title: 'Upload', icon: Upload },
    { type: 'external', href: '/vector-search', title: 'Search', icon: Search },
    { type: 'section', title: 'Communication' },
    { type: 'link', to: '/chat', title: 'Chat', icon: MessageCircle },
    { type: 'external', href: '/history', title: 'History', icon: History },
    { type: 'section', title: 'Automation' },
    { type: 'external', href: '/events', title: 'Events', icon: Calendar },
    { type: 'external', href: '/event-listeners', title: 'Event Listeners', icon: Settings },
    { type: 'section', title: 'Internal' },
    { type: 'link', to: '/tools', title: 'Tools', icon: Cog },
    { type: 'external', href: '/tasks', title: 'Task Queue', icon: Settings },
    { type: 'link', to: '/errors', title: 'Error Logs', icon: AlertTriangle },
    { type: 'section', title: 'Help' },
    { type: 'external', href: '/docs/', title: 'Help', icon: HelpCircle },
  ];

  return (
    <>
      <header className="border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        {/* Desktop Navigation */}
        <div className="hidden md:block">
          <NavigationMenu className="mx-auto max-w-full">
            <NavigationMenuList className="flex-wrap justify-start gap-2 px-4 py-3">
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
                  <NavLink to="/chat" isActive={currentPage === 'chat'}>
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
            </NavigationMenuList>
          </NavigationMenu>
        </div>

        {/* Mobile Navigation */}
        <div className="md:hidden flex items-center justify-between px-4 py-3">
          <div className="text-lg font-semibold">Family Assistant</div>
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="outline" size="icon">
                <Menu className="h-4 w-4" />
                <span className="sr-only">Open navigation menu</span>
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-[300px] sm:w-[400px]">
              <SheetHeader>
                <SheetTitle>Navigation</SheetTitle>
                <SheetDescription>Browse the Family Assistant features</SheetDescription>
              </SheetHeader>
              <nav className="flex flex-col gap-4 mt-6">
                {mobileMenuItems.map((item, index) => {
                  if (item.type === 'section') {
                    return (
                      <div key={index} className="pt-4 first:pt-0">
                        <h4 className="text-sm font-medium text-muted-foreground mb-2">
                          {item.title}
                        </h4>
                      </div>
                    );
                  }

                  const Icon = item.icon!;
                  const isActive = item.type === 'link' && currentPage === item.to?.split('/')[1];

                  if (item.type === 'link') {
                    return (
                      <Link
                        key={index}
                        to={item.to!}
                        className={cn(
                          'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground',
                          isActive && 'bg-accent/50 text-accent-foreground'
                        )}
                      >
                        <Icon className="h-4 w-4" />
                        {item.title}
                      </Link>
                    );
                  }

                  return (
                    <a
                      key={index}
                      href={item.href!}
                      className="flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                    >
                      <Icon className="h-4 w-4" />
                      {item.title}
                    </a>
                  );
                })}
              </nav>
            </SheetContent>
          </Sheet>
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
