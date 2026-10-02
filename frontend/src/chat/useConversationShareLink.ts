import { useCallback, useEffect, useState } from 'react';

export interface ConversationShareLink {
  url: string | null;
  setUrl: (url: string | null) => void;
}

// Own the URL above the responsive headers, but discard it when leaving the conversation.
export function useConversationShareLink(conversationId: string | null): ConversationShareLink {
  const [link, setLink] = useState<{ conversationId: string | null; url: string | null }>({
    conversationId,
    url: null,
  });
  useEffect(() => {
    setLink({ conversationId, url: null });
  }, [conversationId]);
  const setUrl = useCallback(
    (url: string | null) => {
      setLink((current) =>
        current.conversationId === conversationId ? { ...current, url } : current
      );
    },
    [conversationId]
  );
  return { url: link.conversationId === conversationId ? link.url : null, setUrl };
}
