import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

/**
 * Displays tool parameters as structured JSON with copy and rich editor features.
 * Wraps the JsonPayloadViewer functionality but customized for tool arguments display.
 */
const ToolParameterViewer = ({ data, toolName }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const copyTimeoutRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');
  const [isExpanded, setIsExpanded] = useState(true); // Start expanded for tool args
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

  // Initialize editor once when expanded
  useEffect(() => {
    if (!containerRef.current || !isExpanded) {
      return;
    }

    // Prevent double initialization
    if (editorRef.current) {
      return;
    }

    // Clear any existing content before initializing
    containerRef.current.innerHTML = '';

    // Dynamically import and initialize the JSON editor
    const initEditor = async () => {
      try {
        const { JSONEditor } = await import('vanilla-jsoneditor');

        // Double-check we haven't initialized in the meantime
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

    // Cleanup function
    return () => {
      if (editorRef.current) {
        editorRef.current.destroy();
        editorRef.current = null;
      }
      // Clear the container to prevent duplication when remounting
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

  // Handle copy to clipboard
  const handleCopy = async () => {
    // Clear any existing timeout
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

  // Handle expanding the JSON viewer
  const handleToggleExpand = () => {
    setIsExpanded(!isExpanded);
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
        <div style={{ fontSize: '0.9rem', fontWeight: 'bold' }}>
          Arguments{toolName ? ` for ${toolName}` : ''}
        </div>
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
          {/* Always use rich editor */}
          <div ref={containerRef} style={editorContainerStyle} />
        </div>
      )}
    </div>
  );
};

export default ToolParameterViewer;
