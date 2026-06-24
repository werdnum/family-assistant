import { createContext, useContext } from 'react';

/**
 * Mid-run controls shared from ChatApp down into the assistant-ui Thread, where
 * the composer lives. Kept in its own module so Thread doesn't import ChatApp
 * (which would be circular).
 */
export interface ChatControls {
  /**
   * Inject a steering message into the currently-running turn. Resolves true
   * when accepted; false when there is no steerable turn (caller may fall back
   * to sending a normal new message).
   */
  steerStream: (params: { prompt: string }) => Promise<boolean>;
}

export const ChatControlsContext = createContext<ChatControls | null>(null);

export const useChatControls = (): ChatControls | null => useContext(ChatControlsContext);
