import { createContext, useContext } from 'react';

/** Outcome of submitting a steer, so the composer knows whether to clear. */
export type SteerResult = 'accepted' | 'finished' | 'error';

/**
 * Mid-run controls shared from ChatApp down into the assistant-ui Thread, where
 * the composer lives. Kept in its own module so Thread doesn't import ChatApp
 * (which would be circular).
 *
 * While a turn is running the main composer doubles as the steer input, so the
 * steer text lives in the assistant-ui composer (not here); these controls only
 * carry the submit action and the last error.
 */
export interface ChatControls {
  /**
   * Submit ``prompt`` into the running turn and report what happened, so the
   * composer can clear itself on success (``accepted``/``finished``) but keep
   * the text for a retry on ``error``.
   */
  submitSteer: (prompt: string) => Promise<SteerResult>;
  /** Last steer failure message (transient error), shown above the composer. */
  steerError: string | null;
  /** Whether the open conversation has history older than what is loaded. */
  hasOlderMessages: boolean;
  olderMessagesStatus: OlderMessagesStatus;
  /** Extend the loaded history of the open conversation by one page. */
  loadOlderMessages: () => void;
}

export type OlderMessagesStatus = 'idle' | 'loading' | 'failed';

export const ChatControlsContext = createContext<ChatControls | null>(null);

export const useChatControls = (): ChatControls | null => useContext(ChatControlsContext);
