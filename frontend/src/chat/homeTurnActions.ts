import { createContext, useContext } from 'react';

/** A Home message a thread action starts from. */
export interface HomeTurnTarget {
  /** The stored row, once there is one; a reply streamed here has none yet. */
  internalId?: number;
  turnId?: string;
  /** The message's text, for the dialog to quote. */
  quote: string;
}

/** What Home offers on each of its messages: carrying the exchange into a thread. */
export interface HomeTurnActions {
  continueInThread: (target: HomeTurnTarget) => void;
  moveToThread: (target: HomeTurnTarget) => void;
  /**
   * Whether a turn holds a message the user wrote. Only such a turn moves: a
   * thread's echoed outcome or a reminder has nothing of the user's to carry.
   */
  turnHasUserMessage: (turnId: string | undefined) => boolean;
}

export const HomeTurnActionsContext = createContext<HomeTurnActions | null>(null);

/** Null anywhere but Home. */
export const useHomeTurnActions = (): HomeTurnActions | null => useContext(HomeTurnActionsContext);
