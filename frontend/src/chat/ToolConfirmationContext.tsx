import { createContext, useContext } from 'react';

export interface PendingToolConfirmation {
  request_id: string;
  tool_name?: string;
  tool_call_id?: string | null;
  confirmation_prompt?: string;
  args?: Record<string, unknown>;
  created_at?: string | number;
  expires_at?: string | number;
  received_at?: string | number;
  timeout_seconds?: number;
  time_remaining_seconds?: number;
  [key: string]: unknown;
}

interface ToolConfirmationContextType {
  pendingConfirmations: Map<string, PendingToolConfirmation>;
  handleConfirmation: (toolCallId: string, requestId: string, approved: boolean) => Promise<void>;
}

export const ToolConfirmationContext = createContext<ToolConfirmationContextType | null>(null);

export const useToolConfirmation = () => {
  const context = useContext(ToolConfirmationContext);
  if (!context) {
    // Return a default that does nothing when context is not available
    return {
      pendingConfirmations: new Map(),
      handleConfirmation: async () => {},
    };
  }
  return context;
};

export const ToolConfirmationProvider = ToolConfirmationContext.Provider;
