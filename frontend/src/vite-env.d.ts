/// <reference types="vite/client" />
/// <reference types="vitest/globals" />

declare module '*.module.css' {
  const classes: { [key: string]: string };
  export default classes;
}

interface Window {
  FamilyAssistant: {
    mount(rootElement: HTMLElement): void;
    setConversation(conversationId: string): void;
    baseUrl: string;
    staticUrl: string;
    version?: string;
    loaded?: boolean;
  };
}
