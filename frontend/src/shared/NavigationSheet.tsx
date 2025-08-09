import React from 'react';
import { Link } from 'react-router-dom';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { getNavigationItems } from './navigation';
import { cn } from '@/lib/utils';

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

  return (
    <Sheet>
      <SheetTrigger asChild>{children}</SheetTrigger>
      <SheetContent side={side} className="w-[300px] sm:w-[400px]">
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription>{description}</SheetDescription>
        </SheetHeader>
        <nav className="flex flex-col gap-4 mt-6">
          {navigationItems.map((item, index) => {
            if (item.type === 'section') {
              return (
                <div key={index} className="pt-4 first:pt-0">
                  <h4 className="text-sm font-medium text-muted-foreground mb-2">{item.title}</h4>
                </div>
              );
            }

            const Icon = item.icon!;
            const isActive = item.type === 'current';

            if (item.type === 'link' || item.type === 'current') {
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
  );
};

export default NavigationSheet;
