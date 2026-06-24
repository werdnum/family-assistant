import { createContext, useContext } from 'react';

/**
 * Mid-run controls shared from ChatApp down into the assistant-ui Thread, where
 * the composer lives. Kept in its own module so Thread doesn't import ChatApp
 * (which would be circular).
 */
export interface ChatControls {
  /**
   * The current steer-input text. Owned by ChatApp (not the SteerBar) so it
   * survives the SteerBar unmounting when a turn ends: a steer that was never
   * echoed (the turn finished before draining it) is preserved, not lost.
   */
  steerText: string;
  setSteerText: (text: string) => void;
  /**
   * Submit the current steer text into the running turn. Does NOT clear the
   * text — it's cleared only when the matching ``user_input`` echo confirms the
   * turn actually consumed it (see ChatApp.handleStreamingUserInput).
   */
  submitSteer: () => Promise<void>;
}

export const ChatControlsContext = createContext<ChatControls | null>(null);

export const useChatControls = (): ChatControls | null => useContext(ChatControlsContext);
