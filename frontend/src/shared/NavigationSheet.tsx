import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Separator } from '@/components/ui/separator';
import {
  Sheet,
  SheetContent,
  SheetClose,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';
import { getNavigationItems } from './navigation';
import { ThemeToggle } from './ThemeToggle';

interface NavigationSheetProps {
  children: React.ReactNode; // The trigger element
  currentPage?: string;
  title?: string;
  description?: string;
  side?: 'left' | 'right';
}

const NavigationSheet: React.FC<NavigationSheetProps> = ({
  children,
  currentPage,
  title = 'Family Assistant',
  description = 'Navigate to different sections',
  side = 'right',
}) => {
  const navigationItems = getNavigationItems(currentPage);
  const { pathname } = useLocation();

  return (
    <Sheet>
      <SheetTrigger asChild>{children}</SheetTrigger>
      <SheetContent
        side={side}
        className="w-[300px] max-w-[calc(100vw-2rem)] sm:w-[400px] flex flex-col"
      >
        <SheetHeader className="flex-shrink-0">
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription>{description}</SheetDescription>
        </SheetHeader>
        <nav className="flex-1 flex flex-col gap-4 mt-6 overflow-y-auto pr-2">
          {navigationItems.map((item, index) => {
            if (item.type === 'section') {
              return (
                <div key={index} className="pt-4 first:pt-0">
                  <h4 className="text-sm font-medium text-muted-foreground mb-2">{item.title}</h4>
                </div>
              );
            }

            const Icon = item.icon!;
            const target = item.to || item.href;
            const isActive =
              target === '/'
                ? pathname === '/'
                : pathname.replace(/\/$/, '') === target?.replace(/\/$/, '');

            return (
              <SheetClose asChild key={index}>
                <Link
                  to={target!}
                  aria-current={isActive ? 'page' : undefined}
                  className={cn(
                    'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    isActive && 'bg-accent/50 text-accent-foreground'
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {item.title}
                </Link>
              </SheetClose>
            );
          })}

          {/* Theme Toggle Section */}
          <Separator className="my-2" />
          <div className="flex items-center justify-between px-3 py-2">
            <span className="text-sm font-medium">Theme</span>
            <ThemeToggle variant="ghost" size="sm" />
          </div>
        </nav>
      </SheetContent>
    </Sheet>
  );
};

export default NavigationSheet;
