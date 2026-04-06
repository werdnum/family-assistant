import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

const JsonPayloadViewer = ({ data, taskId: _taskId }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const copyTimeoutRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');
  const [isExpanded, setIsExpanded] = useState(false);
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
      mediaQuery.addListener(handleChange);
      return () => mediaQuery.removeListener(handleChange);
    }
  }, []);

  // Initialize editor when expanded
  useEffect(() => {
    if (!containerRef.current || !isExpanded) {
      return;
    }

    if (editorRef.current) {
      return;
    }

    containerRef.current.innerHTML = '';

    const initEditor = async () => {
      try {
        const { JSONEditor } = await import('vanilla-jsoneditor');

        if (editorRef.current) {
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
      if (editorRef.current) {
        editorRef.current.destroy();
        editorRef.current = null;
      }
      if (containerRef.current) {
        containerRef.current.innerHTML = '';
      }
    };
  }, [isExpanded]);

  // Update editor content when data changes
  useEffect(() => {
    if (editorRef.current && isExpanded) {
      try {
        editorRef.current.update({ json: data });
      } catch (error) {
        console.error('Failed to update JSON editor:', error);
      }
    }
  }, [data, isExpanded]);

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
      const jsonString = JSON.stringify(data, null, 2);
      await window.navigator.clipboard.writeText(jsonString);
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
          <div ref={containerRef} style={editorContainerStyle} />
        </div>
      )}
    </div>
  );
};

export default JsonPayloadViewer;
