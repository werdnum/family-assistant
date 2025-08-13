import { createContext, useContext } from 'react';

interface ToolConfirmationContextType {
  pendingConfirmations: Map<string, any>;
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
