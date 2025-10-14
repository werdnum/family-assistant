import React, { useEffect, useRef, useState, CSSProperties } from 'react';
import { Button } from '@/components/ui/button';
import type { JSONEditor } from 'vanilla-jsoneditor';

interface JsonPayloadViewerProps {
  data: Record<string, any> | null;
  taskId: string;
}

const JsonPayloadViewer: React.FC<JsonPayloadViewerProps> = ({ data }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<JSONEditor | null>(null);
  const [copyStatus, setCopyStatus] = useState<string>('');
  const [isExpanded, setIsExpanded] = useState<boolean>(false);
  const [useEditor, setUseEditor] = useState<boolean>(false);
  const [isDarkMode, setIsDarkMode] = useState<boolean>(false);

  // Detect dark mode
  useEffect(() => {
    const checkDarkMode = () => {
      setIsDarkMode(window.matchMedia('(prefers-color-scheme: dark)').matches);
    };

    checkDarkMode();

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = (e: MediaQueryListEvent) => setIsDarkMode(e.matches);

    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, []);

  useEffect(() => {
    if (!containerRef.current || !isExpanded || !useEditor || !data) {
      return;
    }

    let editorInstance: JSONEditor | null = null;

    // Dynamically import and initialize the JSON editor
    const initEditor = async () => {
      try {
        const { JSONEditor: Editor } = await import('vanilla-jsoneditor');
        if (containerRef.current) {
          editorInstance = new Editor({
            target: containerRef.current,
            props: {
              content: { json: data },
              readOnly: true,
              mode: 'text',
              mainMenuBar: false,
              navigationBar: false,
              statusBar: false,
              askToFormat: false,
              escapeControlCharacters: false,
              flattenColumns: true,
            },
          });
          editorRef.current = editorInstance;
        }
      } catch (error) {
        console.error('Failed to load JSON editor:', error);
      }
    };

    initEditor();

    // Cleanup function
    return () => {
      if (editorRef.current) {
        editorRef.current.destroy();
        editorRef.current = null;
      }
    };
  }, [data, isExpanded, useEditor]);

  // Handle copy to clipboard
  const handleCopy = async () => {
    if (!data) {
      return;
    }
    try {
      const jsonString = JSON.stringify(data, null, 2);
      await navigator.clipboard.writeText(jsonString);
      setCopyStatus('Copied!');
      setTimeout(() => setCopyStatus(''), 2000);
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
      setCopyStatus('Copy failed');
      setTimeout(() => setCopyStatus(''), 2000);
    }
  };

  const handleToggleExpand = () => {
    setIsExpanded(!isExpanded);
  };

  const handleUseEditor = () => {
    setUseEditor(true);
  };

  const containerStyle: CSSProperties = {
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
    backgroundColor: isDarkMode ? '#1f2937' : '#f8f9fa',
    marginTop: '0.5rem',
  };

  const headerStyle: CSSProperties = {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '0.5rem',
    borderBottom: isExpanded ? `1px solid ${isDarkMode ? '#374151' : '#ddd'}` : 'none',
    backgroundColor: isDarkMode ? '#374151' : '#e9ecef',
    color: isDarkMode ? '#f9fafb' : '#374151',
  };

  const preStyle: CSSProperties = {
    fontSize: '0.8rem',
    fontFamily: 'monospace',
    backgroundColor: isDarkMode ? '#0f172a' : '#fff',
    color: isDarkMode ? '#e2e8f0' : '#1f2937',
    padding: '0.5rem',
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
    overflow: 'auto',
    maxHeight: '300px',
    margin: 0,
  };

  const editorContainerStyle: CSSProperties = {
    minHeight: '200px',
    maxHeight: '400px',
    overflow: 'auto',
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
  };

  return (
    <div style={containerStyle}>
      <div style={headerStyle}>
        <div style={{ fontSize: '0.9rem', fontWeight: 'bold' }}>Task Payload</div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <Button
            onClick={handleToggleExpand}
            variant="secondary"
            size="sm"
            title="Toggle JSON view"
          >
            {isExpanded ? 'Hide' : 'Show'}
          </Button>
          <Button onClick={handleCopy} variant="default" size="sm" title="Copy JSON to clipboard">
            {copyStatus || 'Copy'}
          </Button>
        </div>
      </div>

      {isExpanded && (
        <div style={{ padding: '0.5rem' }}>
          {!useEditor ? (
            <div>
              <div style={{ marginBottom: '0.5rem' }}>
                <Button
                  onClick={handleUseEditor}
                  variant="secondary"
                  size="sm"
                  title="Load rich editor"
                >
                  Load Rich Editor
                </Button>
              </div>
              <pre style={preStyle}>{JSON.stringify(data, null, 2)}</pre>
            </div>
          ) : (
            <div ref={containerRef} style={editorContainerStyle} />
          )}
        </div>
      )}
    </div>
  );
};

export default JsonPayloadViewer;