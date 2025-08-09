import React, { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';
import { Button } from '@/components/ui/button';
import styles from './DocumentationView.module.css';

const DocumentationView = ({ onBackToList }) => {
  const { filename } = useParams();
  const navigate = useNavigate();
  const [content, setContent] = useState('');
  const [availableDocs, setAvailableDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [docTitle, setDocTitle] = useState('');

  useEffect(() => {
    const fetchContent = async () => {
      try {
        setLoading(true);
        setError(null);

        // Fetch the specific document content
        const contentResponse = await fetch(`/api/documentation/${encodeURIComponent(filename)}`);
        if (!contentResponse.ok) {
          throw new Error(`Failed to fetch document: ${contentResponse.statusText}`);
        }

        const contentData = await contentResponse.json();
        setContent(contentData.content);
        setDocTitle(contentData.filename);

        // Fetch available documents for sidebar
        const docsResponse = await fetch('/api/documentation/');
        if (docsResponse.ok) {
          const docsData = await docsResponse.json();
          setAvailableDocs(docsData);
        }
      } catch (err) {
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

  const handleDocNavigation = (docFilename) => {
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
      {/* Sidebar */}
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

      {/* Main content */}
      <div className={styles.content}>
        <div className={styles.contentHeader}>
          <h1>{docTitle.replace(/\.md$/, '').replace(/_/g, ' ')}</h1>
        </div>

        <div className={styles.markdownContent}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize]}
            components={{
              // Custom link component to handle internal links
              a: ({ node: _node, href, children, ...props }) => {
                // If it's a relative link to another doc, handle it internally
                if (href && href.endsWith('.md') && !href.startsWith('http')) {
                  return (
                    <Link to={`/docs/${encodeURIComponent(href)}`} {...props}>
                      {children}
                    </Link>
                  );
                }
                // External links open in new tab
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
              // Style code blocks
              pre: ({ node: _node, children, ...props }) => (
                <pre className={styles.codeBlock} {...props}>
                  {children}
                </pre>
              ),
              // Style inline code
              code: ({ node: _node, inline, children, ...props }) => (
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
