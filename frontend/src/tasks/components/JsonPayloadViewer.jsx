import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

const JsonPayloadViewer = ({ data, taskId: _taskId }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');
  const [isExpanded, setIsExpanded] = useState(false);
  const [useEditor, setUseEditor] = useState(false);
  const [isDarkMode, setIsDarkMode] = useState(false);

  // Detect dark mode
  useEffect(() => {
    const checkDarkMode = () => {
      setIsDarkMode(window.matchMedia('(prefers-color-scheme: dark)').matches);
    };

    checkDarkMode();

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = (e) => setIsDarkMode(e.matches);

    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handleChange);
      return () => mediaQuery.removeEventListener('change', handleChange);
    } else {
      // Fallback for older browsers
      mediaQuery.addListener(handleChange);
      return () => mediaQuery.removeListener(handleChange);
    }
  }, []);

  useEffect(() => {
    if (!containerRef.current || !isExpanded || !useEditor) {
      return;
    }

    // Dynamically import and initialize the JSON editor
    const initEditor = async () => {
      try {
        const { JSONEditor } = await import('vanilla-jsoneditor');
        editorRef.current = new JSONEditor({
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
    try {
      const jsonString = JSON.stringify(data, null, 2);
      await window.navigator.clipboard.writeText(jsonString);
      setCopyStatus('Copied!');
      window.setTimeout(() => setCopyStatus(''), 2000);
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
      setCopyStatus('Copy failed');
      window.setTimeout(() => setCopyStatus(''), 2000);
    }
  };

  // Handle expanding the JSON viewer
  const handleToggleExpand = () => {
    setIsExpanded(!isExpanded);
  };

  // Handle switching to rich editor
  const handleUseEditor = () => {
    setUseEditor(true);
  };

  // Dynamic styles based on dark mode
  const containerStyle = {
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
    backgroundColor: isDarkMode ? '#1f2937' : '#f8f9fa',
    marginTop: '0.5rem',
  };

  const headerStyle = {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '0.5rem',
    borderBottom: isExpanded ? `1px solid ${isDarkMode ? '#374151' : '#ddd'}` : 'none',
    backgroundColor: isDarkMode ? '#374151' : '#e9ecef',
    color: isDarkMode ? '#f9fafb' : '#374151',
  };

  const preStyle = {
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

  const editorContainerStyle = {
    minHeight: '200px',
    maxHeight: '400px',
    overflow: 'auto',
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
  };

  return (
    <div style={containerStyle}>
      {/* Control buttons */}
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

      {/* JSON content - only render when expanded */}
      {isExpanded && (
        <div style={{ padding: '0.5rem' }}>
          {!useEditor ? (
            // Simple text view by default
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
            // Rich editor container
            <div ref={containerRef} style={editorContainerStyle} />
          )}
        </div>
      )}
    </div>
  );
};

export default JsonPayloadViewer;
