import React, { useEffect, useRef, useState } from 'react';

const JsonPayloadViewer = ({ data, taskId: _taskId }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');
  const [isExpanded, setIsExpanded] = useState(false);
  const [useEditor, setUseEditor] = useState(false);

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

  return (
    <div
      style={{
        border: '1px solid #ddd',
        borderRadius: '3px',
        backgroundColor: '#f8f9fa',
        marginTop: '0.5rem',
      }}
    >
      {/* Control buttons */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '0.5rem',
          borderBottom: isExpanded ? '1px solid #ddd' : 'none',
          backgroundColor: '#e9ecef',
        }}
      >
        <div style={{ fontSize: '0.9rem', fontWeight: 'bold' }}>Task Payload</div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <button
            onClick={handleToggleExpand}
            style={{
              padding: '0.25rem 0.5rem',
              fontSize: '0.8rem',
              backgroundColor: '#6c757d',
              color: 'white',
              border: 'none',
              borderRadius: '3px',
              cursor: 'pointer',
            }}
            title="Toggle JSON view"
          >
            {isExpanded ? 'Hide' : 'Show'}
          </button>
          <button
            onClick={handleCopy}
            style={{
              padding: '0.25rem 0.5rem',
              fontSize: '0.8rem',
              backgroundColor: '#007bff',
              color: 'white',
              border: 'none',
              borderRadius: '3px',
              cursor: 'pointer',
            }}
            title="Copy JSON to clipboard"
          >
            {copyStatus || 'Copy'}
          </button>
        </div>
      </div>

      {/* JSON content - only render when expanded */}
      {isExpanded && (
        <div style={{ padding: '0.5rem' }}>
          {!useEditor ? (
            // Simple text view by default
            <div>
              <div style={{ marginBottom: '0.5rem' }}>
                <button
                  onClick={handleUseEditor}
                  style={{
                    padding: '0.25rem 0.5rem',
                    fontSize: '0.8rem',
                    backgroundColor: '#28a745',
                    color: 'white',
                    border: 'none',
                    borderRadius: '3px',
                    cursor: 'pointer',
                  }}
                  title="Load rich editor"
                >
                  Load Rich Editor
                </button>
              </div>
              <pre
                style={{
                  fontSize: '0.8rem',
                  fontFamily: 'monospace',
                  backgroundColor: '#fff',
                  padding: '0.5rem',
                  border: '1px solid #ddd',
                  borderRadius: '3px',
                  overflow: 'auto',
                  maxHeight: '300px',
                  margin: 0,
                }}
              >
                {JSON.stringify(data, null, 2)}
              </pre>
            </div>
          ) : (
            // Rich editor container
            <div
              ref={containerRef}
              style={{
                minHeight: '200px',
                maxHeight: '400px',
                overflow: 'auto',
                border: '1px solid #ddd',
                borderRadius: '3px',
              }}
            />
          )}
        </div>
      )}
    </div>
  );
};

export default JsonPayloadViewer;
