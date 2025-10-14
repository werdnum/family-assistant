import React, { useState, useEffect, FC } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';
import { Button } from '@/components/ui/button';
import styles from './DocumentationView.module.css';

interface DocumentationViewProps {
  onBackToList: () => void;
}

interface DocumentationContent {
  content: string;
  filename: string;
}

const DocumentationView: FC<DocumentationViewProps> = ({ onBackToList }) => {
  const { filename } = useParams<{ filename: string }>();
  const navigate = useNavigate();
  const [content, setContent] = useState<string>('');
  const [availableDocs, setAvailableDocs] = useState<string[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [docTitle, setDocTitle] = useState<string>('');

  useEffect(() => {
    const fetchContent = async () => {
      try {
        setLoading(true);
        setError(null);

        const contentResponse = await fetch(`/api/documentation/${encodeURIComponent(filename!)}`);
        if (!contentResponse.ok) {
          throw new Error(`Failed to fetch document: ${contentResponse.statusText}`);
        }

        const contentData: DocumentationContent = await contentResponse.json();
        setContent(contentData.content);
        setDocTitle(contentData.filename);

        const docsResponse = await fetch('/api/documentation/');
        if (docsResponse.ok) {
          const docsData: string[] = await docsResponse.json();
          setAvailableDocs(docsData);
        }
      } catch (err: any) {
        console.error('Error fetching documentation:', err);
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (filename) {
      fetchContent();
    }
  }, [filename]);

  const handleDocNavigation = (docFilename: string) => {
    navigate(`/docs/${encodeURIComponent(docFilename)}`);
  };

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.loading}>Loading documentation...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>
          <h2>Error</h2>
          <p>Error loading documentation: {error}</p>
          <Button onClick={onBackToList} variant="outline">
            ← Back to Documentation List
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.sidebar}>
        <div className={styles.sidebarHeader}>
          <Button onClick={onBackToList} variant="outline">
            ← Documentation
          </Button>
        </div>

        {availableDocs.length > 0 && (
          <div className={styles.docNav}>
            <h3>Available Docs</h3>
            <ul className={styles.docList}>
              {availableDocs.map((docFilename) => (
                <li key={docFilename}>
                  <Button
                    onClick={() => handleDocNavigation(docFilename)}
                    variant={docFilename === filename ? 'default' : 'ghost'}
                    size="sm"
                    className={styles.docNavItem}
                  >
                    {docFilename.replace(/\.md$/, '').replace(/_/g, ' ')}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className={styles.content}>
        <div className={styles.contentHeader}>
          <h1>{docTitle.replace(/\.md$/, '').replace(/_/g, ' ')}</h1>
        </div>

        <div className={styles.markdownContent}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize]}
            components={{
              a: ({ href, children, ...props }) => {
                if (href && href.endsWith('.md') && !href.startsWith('http')) {
                  return (
                    <Link to={`/docs/${encodeURIComponent(href)}`} {...props}>
                      {children}
                    </Link>
                  );
                }
                if (href && href.startsWith('http')) {
                  return (
                    <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                      {children}
                    </a>
                  );
                }
                return (
                  <a href={href} {...props}>
                    {children}
                  </a>
                );
              },
              pre: ({ children, ...props }) => (
                <pre className={styles.codeBlock} {...props}>
                  {children}
                </pre>
              ),
              code: ({ inline, children, ...props }) => (
                <code className={inline ? styles.inlineCode : ''} {...props}>
                  {children}
                </code>
              ),
            }}
          >
            {content}
          </ReactMarkdown>
        </div>
      </div>
    </div>
  );
};

export default DocumentationView;