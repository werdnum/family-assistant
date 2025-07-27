import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';

// Secure markdown renderer for chat messages
export const MarkdownText = ({ text }) => {
  if (!text) return null;
  
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
          code: ({ inline, className, children }) => {
            if (inline) {
              return <code className="inline-code">{children}</code>;
            }
            return (
              <pre className="code-block">
                <code className={className}>{children}</code>
              </pre>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
};
