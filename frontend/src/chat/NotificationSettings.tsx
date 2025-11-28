import { Bell, BellOff } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';

interface NotificationSettingsProps {
  enabled: boolean;
  onEnabledChange: (enabled: boolean) => void;
  permission: NotificationPermission;
  onRequestPermission: () => void;
  isSupported: boolean;
}

export const NotificationSettings: React.FC<NotificationSettingsProps> = ({
  enabled,
  onEnabledChange,
  permission,
  onRequestPermission,
  isSupported,
}) => {
  if (!isSupported) {
    return null; // Don't show if notifications aren't supported
  }

  const getPermissionBadge = () => {
    switch (permission) {
      case 'granted':
        return (
          <Badge variant="default" className="bg-green-600">
            Granted
          </Badge>
        );
      case 'denied':
        return <Badge variant="destructive">Blocked</Badge>;
      default:
        return <Badge variant="secondary">Not Set</Badge>;
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="h-9 w-9 p-0">
          {enabled && permission === 'granted' ? (
            <Bell className="h-4 w-4" />
          ) : (
            <BellOff className="h-4 w-4" />
          )}
          <span className="sr-only">Notification settings</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Notification Settings</DropdownMenuLabel>
        <DropdownMenuSeparator />

        <div className="p-2 space-y-4">
          {/* Enable/Disable Toggle */}
          <div className="flex items-center justify-between">
            <Label htmlFor="notifications-enabled" className="text-sm font-medium">
              Enable Notifications
            </Label>
            <Switch
              id="notifications-enabled"
              checked={enabled}
              onCheckedChange={onEnabledChange}
              disabled={permission === 'denied'}
            />
          </div>

          {/* Permission Status */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">Permission Status</span>
              {getPermissionBadge()}
            </div>

            {/* Request Permission Button */}
            {permission === 'default' && (
              <Button onClick={onRequestPermission} variant="outline" size="sm" className="w-full">
                Allow Notifications
              </Button>
            )}

            {permission === 'denied' && (
              <p className="text-xs text-muted-foreground">
                Notifications are blocked. Please enable them in your browser settings.
              </p>
            )}

            {permission === 'granted' && enabled && (
              <p className="text-xs text-muted-foreground">
                You'll receive notifications when new messages arrive while you're away.
              </p>
            )}
          </div>

          {/* Help Text */}
          <DropdownMenuSeparator />
          <div className="text-xs text-muted-foreground space-y-1">
            <p>Notifications appear when:</p>
            <ul className="list-disc pl-4 space-y-0.5">
              <li>You receive a new message</li>
              <li>The chat tab is hidden or in the background</li>
              <li>Or the message is in a different conversation</li>
            </ul>
          </div>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
};
