import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

export const LARGE_PAYLOAD_THRESHOLD = 10240; // 10KB

const JsonPayloadViewer = ({ data, taskId: _taskId }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const copyTimeoutRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');
  const [isExpanded, setIsExpanded] = useState(false);
  const [useEditor, setUseEditor] = useState(false);
  const [isDarkMode, setIsDarkMode] = useState(false);

  const isLargePayload = useMemo(
    () => JSON.stringify(data).length > LARGE_PAYLOAD_THRESHOLD,
    [data]
  );

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
      mediaQuery.addListener(handleChange);
      return () => mediaQuery.removeListener(handleChange);
    }
  }, []);

  const showEditor = isExpanded && (useEditor || !isLargePayload);

  // Initialize editor when expanded and editor mode is active
  useEffect(() => {
    if (!containerRef.current || !showEditor) {
      return;
    }

    if (editorRef.current) {
      return;
    }

    containerRef.current.innerHTML = '';

    let isCancelled = false;

    const initEditor = async () => {
      try {
        const { JSONEditor } = await import('vanilla-jsoneditor');

        if (isCancelled || editorRef.current || !containerRef.current) {
          return;
        }

        editorRef.current = new JSONEditor({
          target: containerRef.current,
          props: {
            content: { json: data },
            readOnly: true,
            mode: 'tree',
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

    return () => {
      isCancelled = true;
      if (editorRef.current) {
        editorRef.current.destroy();
        editorRef.current = null;
      }
      if (containerRef.current) {
        containerRef.current.innerHTML = '';
      }
    };
  }, [data, showEditor]);

  // Update editor content when data changes
  useEffect(() => {
    if (editorRef.current && showEditor) {
      try {
        editorRef.current.update({ json: data });
      } catch (error) {
        console.error('Failed to update JSON editor:', error);
      }
    }
  }, [data, showEditor]);

  // Cleanup copy timeout on unmount
  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    if (copyTimeoutRef.current) {
      window.clearTimeout(copyTimeoutRef.current);
    }

    try {
      await window.navigator.clipboard.writeText(JSON.stringify(data, null, 2));
      setCopyStatus('Copied!');
      copyTimeoutRef.current = window.setTimeout(() => setCopyStatus(''), 2000);
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
      setCopyStatus('Copy failed');
      copyTimeoutRef.current = window.setTimeout(() => setCopyStatus(''), 2000);
    }
  };

  const handleToggleExpand = () => {
    setIsExpanded(!isExpanded);
  };

  const containerStyle = {
    border: `1px solid ${isDarkMode ? '#374151' : '#ddd'}`,
    borderRadius: '3px',
    backgroundColor: isDarkMode ? '#1f2937' : '#f8f9fa',
    marginTop: '0.5rem',
    minWidth: 0,
    overflow: 'hidden',
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
          {isLargePayload && !useEditor ? (
            <div>
              <div style={{ marginBottom: '0.5rem' }}>
                <Button
                  onClick={() => setUseEditor(true)}
                  variant="secondary"
                  size="sm"
                  title="Load rich editor (large payload)"
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
