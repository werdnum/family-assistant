/**
 * Voice mode page component.
 *
 * Provides a dedicated interface for voice conversations with the assistant
 * using the Gemini Live API.
 */

import { Link } from 'react-router-dom';
import { SESSION_CONFIG } from './types';
import { StatusIndicator } from './StatusIndicator';
import { TranscriptPanel } from './TranscriptPanel';
import { useGeminiLive } from './useGeminiLive';
import { VoiceControls } from './VoiceControls';

/**
 * Format session duration for display.
 */
function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${minutes}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Calculate remaining session time.
 */
function getRemainingTime(duration: number): number {
  return Math.max(0, SESSION_CONFIG.MAX_DURATION_MINUTES * 60 - duration);
}

/**
 * Session timer display component.
 */
function SessionTimer({ duration, isActive }: { duration: number; isActive: boolean }) {
  if (!isActive) {
    return null;
  }

  const remaining = getRemainingTime(duration);
  const isLowTime = remaining < 60; // Less than 1 minute

  return (
    <div
      className={`text-sm font-mono ${
        isLowTime ? 'text-red-500 animate-pulse' : 'text-gray-500 dark:text-gray-400'
      }`}
    >
      Session: {formatDuration(duration)} / {SESSION_CONFIG.MAX_DURATION_MINUTES}:00
      {isLowTime && <span className="ml-2 text-xs">(ending soon)</span>}
    </div>
  );
}

/**
 * Error display component.
 */
function ErrorDisplay({ error, onDismiss }: { error: string; onDismiss: () => void }) {
  return (
    <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 mb-4">
      <div className="flex items-start gap-3">
        <svg
          className="w-5 h-5 text-red-500 mt-0.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
        <div className="flex-1">
          <h4 className="text-sm font-medium text-red-800 dark:text-red-200">Connection Error</h4>
          <p className="text-sm text-red-600 dark:text-red-300 mt-1">{error}</p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="text-red-500 hover:text-red-700"
          aria-label="Dismiss error"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}

/**
 * Main voice page component.
 */
export function VoicePage() {
  const { sessionState, transcripts, connect, disconnect } = useGeminiLive();

  const handleStartCall = () => {
    connect();
  };

  const handleEndCall = () => {
    disconnect();
  };

  const handleDismissError = () => {
    // Trigger reconnect attempt which will clear the error
    disconnect();
  };

  const isSessionActive = sessionState.connection === 'connected';

  return (
    <div className="min-h-screen bg-gradient-to-b from-gray-50 to-gray-100 dark:from-gray-900 dark:to-gray-800">
      {/* Header */}
      <header className="bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 px-4 py-3">
        <div className="max-w-4xl mx-auto flex items-center justify-between">
          <Link
            to="/chat"
            className="flex items-center gap-2 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M10 19l-7-7m0 0l7-7m-7 7h18"
              />
            </svg>
            Back to Chat
          </Link>

          <h1 className="text-xl font-semibold text-gray-800 dark:text-white">Voice Mode</h1>

          <SessionTimer duration={sessionState.sessionDuration} isActive={isSessionActive} />
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-4xl mx-auto p-4">
        {/* Error display */}
        {sessionState.error && (
          <ErrorDisplay error={sessionState.error} onDismiss={handleDismissError} />
        )}

        {/* Status indicator */}
        <div className="flex justify-center py-12">
          <StatusIndicator
            connectionState={sessionState.connection}
            activityState={sessionState.activity}
          />
        </div>

        {/* Transcript panel */}
        <div className="bg-white dark:bg-gray-900 rounded-lg shadow-md border border-gray-200 dark:border-gray-700 mb-8">
          <TranscriptPanel transcripts={transcripts} />
        </div>

        {/* Controls */}
        <div className="flex justify-center pb-8">
          <VoiceControls
            connectionState={sessionState.connection}
            onStartCall={handleStartCall}
            onEndCall={handleEndCall}
          />
        </div>

        {/* Info section */}
        <div className="text-center text-sm text-gray-500 dark:text-gray-400 space-y-1">
          <p>Voice sessions are limited to {SESSION_CONFIG.MAX_DURATION_MINUTES} minutes.</p>
          <p>Transcripts are not saved after ending the call.</p>
        </div>
      </main>
    </div>
  );
}

export default VoicePage;
