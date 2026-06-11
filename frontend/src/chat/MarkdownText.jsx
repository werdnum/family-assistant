import React, { createContext, useContext } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

const PreContext = createContext(false);

// Secure markdown renderer for chat messages
export const MarkdownText = ({ text }) => {
  if (!text) {
    return null;
  }

  return (
    <div className="markdown-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          // Custom components for better styling
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          pre: ({ children }) => {
            return (
              <pre className="code-block">
                <PreContext.Provider value={true}>
                  {children}
                </PreContext.Provider>
              </pre>
            );
          },
          code: ({ className, children }) => {
            const isInsidePre = useContext(PreContext);

            // In react-markdown v10, the `inline` prop is no longer provided.
            // We use PreContext to determine if we're inside a <pre> block.
            if (!isInsidePre) {
              return <code className="inline-code">{children}</code>;
            }

            return (
              <code className={className}>{children}</code>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
};
