import {
  ChevronDownIcon,
  ChevronUpIcon,
  GlobeIcon,
  Loader2Icon,
  Maximize2Icon,
  MonitorIcon,
} from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { BrowserViewFrame } from './types';

export function parseBrowserViewFrame(raw: unknown): BrowserViewFrame | null {
  if (!raw || typeof raw !== 'object') {
    return null;
  }
  const obj = raw as Record<string, unknown>;
  if (typeof obj.frame_id !== 'string' || typeof obj.image_url !== 'string') {
    return null;
  }
  const backendKind =
    obj.backend_kind === 'remote' || obj.backend_kind === 'local' ? obj.backend_kind : undefined;
  return {
    frame_id: obj.frame_id,
    url: typeof obj.url === 'string' ? obj.url : '',
    captured_at: typeof obj.captured_at === 'string' ? obj.captured_at : new Date().toISOString(),
    tool_name: typeof obj.tool_name === 'string' ? obj.tool_name : null,
    backend_kind: backendKind,
    width: typeof obj.width === 'number' ? obj.width : null,
    height: typeof obj.height === 'number' ? obj.height : null,
    image_url: obj.image_url,
  };
}

function formatDisplayUrl(rawUrl: string): string {
  const trimmed = rawUrl.trim();
  if (!trimmed) {
    return 'about:blank';
  }
  return trimmed.replace(/^https?:\/\//i, '');
}

function formatToolLabel(toolName: string | null | undefined): string | null {
  if (!toolName) {
    return null;
  }
  return toolName.replace(/^browser_/, '').replace(/_/g, ' ');
}

export interface LiveBrowserViewProps {
  frame: BrowserViewFrame | null;
  isRunning: boolean;
  isBrowserProfile: boolean;
  compact?: boolean;
}

export const LiveBrowserView: React.FC<LiveBrowserViewProps> = ({
  frame,
  isRunning,
  isBrowserProfile,
  compact = false,
}) => {
  const [collapsed, setCollapsed] = useState(false);
  const [expandedOpen, setExpandedOpen] = useState(false);
  const [settledUrl, setSettledUrl] = useState<string | null>(frame?.image_url ?? null);

  useEffect(() => {
    if (!frame) {
      setSettledUrl(null);
      setExpandedOpen(false);
    }
  }, [frame]);

  if (!frame && !(isBrowserProfile && isRunning)) {
    return null;
  }

  if (!frame) {
    return (
      <div
        className="flex-shrink-0 border-b bg-muted/30 px-3 py-2"
        data-testid="live-browser-waiting"
      >
        <div className="max-w-3xl mx-auto flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2Icon className="h-3.5 w-3.5 animate-spin text-primary" aria-hidden="true" />
          <span className="font-medium text-foreground">Live browser</span>
          <span>Starting session…</span>
        </div>
      </div>
    );
  }

  const displayUrl = formatDisplayUrl(frame.url);
  const toolLabel = formatToolLabel(frame.tool_name);
  const showPreviousUnderlay = settledUrl !== null && settledUrl !== frame.image_url;

  return (
    <div className="flex-shrink-0 border-b bg-muted/20 px-3 py-2" data-testid="live-browser-view">
      <div className="max-w-3xl mx-auto rounded-lg border bg-background shadow-xs overflow-hidden">
        <div className="flex items-center gap-2 px-2.5 py-1.5 border-b bg-muted/40 min-w-0">
          <span
            className="inline-flex items-center gap-1.5 text-xs font-medium flex-shrink-0"
            data-testid="live-browser-status"
          >
            {isRunning ? (
              <>
                <span className="relative flex h-2 w-2" aria-hidden="true">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
                </span>
                <span className="text-emerald-700 dark:text-emerald-400">Live</span>
              </>
            ) : (
              <>
                <MonitorIcon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                <span className="text-muted-foreground">Browser</span>
              </>
            )}
          </span>

          {frame.backend_kind && (
            <Badge
              variant="outline"
              className="text-[10px] px-1.5 py-0 h-4 font-normal capitalize flex-shrink-0"
              data-testid="live-browser-backend-badge"
            >
              {frame.backend_kind}
            </Badge>
          )}

          <div
            className="flex items-center gap-1.5 min-w-0 flex-1 rounded bg-background/80 border px-2 py-0.5 text-[11px] text-muted-foreground"
            title={frame.url || 'about:blank'}
            data-testid="live-browser-url"
          >
            <GlobeIcon className="h-3 w-3 flex-shrink-0 opacity-70" aria-hidden="true" />
            <span className="truncate font-mono">{displayUrl}</span>
          </div>

          {toolLabel && (
            <span
              className="hidden sm:inline-block text-[11px] text-muted-foreground capitalize flex-shrink-0"
              data-testid="live-browser-tool"
            >
              {toolLabel}
            </span>
          )}

          <div className="flex items-center gap-0.5 flex-shrink-0">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={() => setExpandedOpen(true)}
              aria-label="Expand live browser view"
              title="Expand live browser view"
              data-testid="live-browser-expand"
            >
              <Maximize2Icon className="h-3.5 w-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={() => setCollapsed((prev) => !prev)}
              aria-label={collapsed ? 'Show live browser view' : 'Hide live browser view'}
              title={collapsed ? 'Show live browser view' : 'Hide live browser view'}
              data-testid="live-browser-toggle"
            >
              {collapsed ? (
                <ChevronDownIcon className="h-3.5 w-3.5" />
              ) : (
                <ChevronUpIcon className="h-3.5 w-3.5" />
              )}
            </Button>
          </div>
        </div>

        {!collapsed && (
          <button
            type="button"
            onClick={() => setExpandedOpen(true)}
            className={`relative block w-full bg-muted/30 overflow-hidden cursor-zoom-in focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
              compact ? 'max-h-52' : 'max-h-64'
            }`}
            aria-label="Open full-size live browser screenshot"
            data-testid="live-browser-viewport"
          >
            {showPreviousUnderlay && (
              <img
                src={settledUrl}
                alt=""
                aria-hidden="true"
                className={`absolute inset-0 w-full h-full object-contain pointer-events-none ${
                  compact ? 'max-h-52' : 'max-h-64'
                }`}
              />
            )}
            <img
              src={frame.image_url}
              alt={`Live browser view of ${frame.url || 'current page'}`}
              onLoad={() => setSettledUrl(frame.image_url)}
              className={`relative block w-full object-contain mx-auto ${
                compact ? 'max-h-52' : 'max-h-64'
              }`}
              data-testid="live-browser-image"
            />
          </button>
        )}
      </div>

      <Dialog open={expandedOpen} onOpenChange={setExpandedOpen}>
        <DialogContent
          className="max-w-5xl w-[calc(100%-2rem)] p-4 gap-3"
          data-testid="live-browser-fullscreen-dialog"
        >
          <DialogHeader className="pr-8">
            <DialogTitle className="flex items-center gap-2 text-sm">
              {isRunning ? (
                <span className="inline-flex items-center gap-1.5 text-emerald-700 dark:text-emerald-400">
                  <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
                  Live browser
                </span>
              ) : (
                <span>Browser view</span>
              )}
              {frame.backend_kind && (
                <Badge variant="outline" className="text-[11px] font-normal capitalize">
                  {frame.backend_kind}
                </Badge>
              )}
              {toolLabel && (
                <span className="text-xs font-normal text-muted-foreground capitalize">
                  · {toolLabel}
                </span>
              )}
            </DialogTitle>
            <DialogDescription className="text-xs font-mono truncate">
              {frame.url || 'about:blank'}
            </DialogDescription>
          </DialogHeader>
          <div className="relative rounded-md border bg-muted/30 overflow-hidden">
            {showPreviousUnderlay && (
              <img
                src={settledUrl}
                alt=""
                aria-hidden="true"
                className="absolute inset-0 w-full h-full max-h-[75dvh] object-contain pointer-events-none"
              />
            )}
            <img
              src={frame.image_url}
              alt={`Full-size live browser view of ${frame.url || 'current page'}`}
              onLoad={() => setSettledUrl(frame.image_url)}
              className="relative block w-full max-h-[75dvh] object-contain mx-auto"
              data-testid="live-browser-fullscreen-image"
            />
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
};
