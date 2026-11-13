import { createContext, useContext } from 'react';
import type { PendingDelegation } from './usePendingDelegations';
import type { ModelTier } from './profilesContext';

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
  /** Background delegations the open conversation is waiting on. */
  pendingDelegations: PendingDelegation[];
  /**
   * Set while the open conversation's history has not loaded: the thread shows
   * this instead of a transcript, and sending is refused until it clears.
   */
  conversationLoadStatus: ConversationLoadStatus | null;
  /** Ask again for the open conversation's history after a failed load. */
  retryConversationLoad: () => void;
  /**
   * In Home, where every message starts a thread: picking another assistant
   * for it, and why a start failed. Null elsewhere.
   */
  sendAsThread: SendAsThreadControls | null;
}

export interface SendAsThreadProfile {
  id: string;
  label: string;
  /** Intelligence levels the profile offers, in configured order. */
  tiers: ModelTier[];
  /** Specific models the profile may also run on. */
  presets: ModelTier[];
  /** The tier the profile runs at when none is named. */
  defaultTierId: string | null;
}

export interface SendAsThreadControls {
  /** The profiles a thread can be started on, other than Home's own. */
  profiles: SendAsThreadProfile[];
  /**
   * Make the composer's next send start a thread on this profile. `tierId` is
   * a model picked for that send, null for the profile's default; left out,
   * Home's own choice carries over where the profile offers it.
   */
  prepare: (profileId: string, tierId?: string | null) => void;
  /** Why the last send-as-thread didn't start one; its message is back in the composer. */
  error: string | null;
}

export type OlderMessagesStatus = 'idle' | 'loading' | 'failed';

export type ConversationLoadStatus = 'loading' | 'failed';

export const ChatControlsContext = createContext<ChatControls | null>(null);

export const useChatControls = (): ChatControls | null => useContext(ChatControlsContext);
