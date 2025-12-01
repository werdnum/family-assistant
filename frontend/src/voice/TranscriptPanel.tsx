/**
 * Transcript panel for voice mode.
 *
 * Displays a scrolling list of conversation turns with timestamps.
 * Auto-scrolls to show the latest message.
 */

import { useEffect, useRef } from 'react';
import { ToolFallback } from '../chat/ToolUI';
import type { TranscriptEntry } from './types';

interface TranscriptPanelProps {
  transcripts: TranscriptEntry[];
  className?: string;
}

/**
 * Format a timestamp for display.
 */
function formatTimestamp(date: Date): string {
  return date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * Single transcript entry component.
 */
function TranscriptItem({ entry }: { entry: TranscriptEntry }) {
  // Tool entries use the existing ToolFallback component
  if (entry.role === 'tool') {
    return (
      <div className="py-2">
        <ToolFallback
          toolName={entry.toolName || entry.text}
          args={entry.toolArgs || {}}
          result={entry.toolResult}
          status={{
            type: entry.toolStatus === 'error' ? 'incomplete' : entry.toolStatus || 'running',
            reason: entry.toolStatus === 'error' ? 'error' : undefined,
          }}
          attachments={[]}
        />
      </div>
    );
  }

  const isUser = entry.role === 'user';

  return (
    <div
      className={`py-3 px-4 rounded-lg ${
        isUser ? 'bg-blue-50 dark:bg-blue-900/20' : 'bg-gray-50 dark:bg-gray-800/50'
      } ${!entry.isFinal ? 'opacity-60' : ''}`}
    >
      <div className="flex items-center gap-2 mb-1">
        <span
          className={`text-sm font-medium ${
            isUser ? 'text-blue-700 dark:text-blue-300' : 'text-gray-700 dark:text-gray-300'
          }`}
        >
          {isUser ? 'You' : 'Assistant'}
        </span>
        <span className="text-xs text-gray-400">{formatTimestamp(entry.timestamp)}</span>
        {!entry.isFinal && <span className="text-xs text-gray-400 italic">(transcribing...)</span>}
      </div>
      <p className="text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{entry.text}</p>
    </div>
  );
}

/**
 * Empty state when no transcripts are available.
 */
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-gray-400 py-12">
      <svg
        className="w-16 h-16 mb-4 opacity-50"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1.5}
          d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
        />
      </svg>
      <p className="text-lg">Start talking to see transcripts</p>
      <p className="text-sm mt-1">Your conversation will appear here</p>
    </div>
  );
}

/**
 * Transcript panel component displaying conversation history.
 */
export function TranscriptPanel({ transcripts, className = '' }: TranscriptPanelProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new transcripts are added
  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [transcripts]);

  return (
    <div className={`flex flex-col ${className}`}>
      <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-300 px-4 py-2 border-b border-gray-200 dark:border-gray-700">
        Transcript
      </h3>

      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-y-auto p-4 space-y-3 min-h-[200px] max-h-[400px]"
      >
        {transcripts.length === 0 ? (
          <EmptyState />
        ) : (
          <>
            {transcripts.map((entry) => (
              <TranscriptItem key={entry.id} entry={entry} />
            ))}
            <div ref={bottomRef} />
          </>
        )}
      </div>
    </div>
  );
}

export default TranscriptPanel;
