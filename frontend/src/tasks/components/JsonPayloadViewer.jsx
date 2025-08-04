import React, { useEffect, useRef, useState } from 'react';
import { JSONEditor } from 'vanilla-jsoneditor';

const JsonPayloadViewer = ({ data, taskId: _taskId }) => {
  const containerRef = useRef(null);
  const editorRef = useRef(null);
  const [copyStatus, setCopyStatus] = useState('');

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    // Initialize the JSON editor
    editorRef.current = new JSONEditor({
      target: containerRef.current,
      props: {
        content: { json: data },
        readOnly: true,
        mode: 'text', // Start in text mode for better viewing
        mainMenuBar: false,
        navigationBar: false,
        statusBar: false,
        askToFormat: false,
        escapeControlCharacters: false,
        flattenColumns: true,
      },
    });

    // Cleanup function
    return () => {
      if (editorRef.current) {
        editorRef.current.destroy();
        editorRef.current = null;
      }
    };
  }, [data]);

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

  // Toggle between text and tree view modes
  const toggleMode = () => {
    if (editorRef.current) {
      const currentContent = editorRef.current.get();
      const currentMode = editorRef.current.mode;
      const newMode = currentMode === 'text' ? 'tree' : 'text';

      editorRef.current.updateProps({
        mode: newMode,
        content: currentContent,
      });
    }
  };

  return (
    <div
      style={{
        border: '1px solid #ddd',
        borderRadius: '3px',
        backgroundColor: '#f8f9fa',
      }}
    >
      {/* Control buttons */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '0.5rem',
          borderBottom: '1px solid #ddd',
          backgroundColor: '#e9ecef',
        }}
      >
        <div style={{ fontSize: '0.9rem', fontWeight: 'bold' }}>Task Payload</div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <button
            onClick={toggleMode}
            style={{
              padding: '0.25rem 0.5rem',
              fontSize: '0.8rem',
              backgroundColor: '#6c757d',
              color: 'white',
              border: 'none',
              borderRadius: '3px',
              cursor: 'pointer',
            }}
            title="Toggle between text and tree view"
          >
            Toggle View
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

      {/* JSON Editor container */}
      <div
        ref={containerRef}
        style={{
          minHeight: '200px',
          maxHeight: '400px',
          overflow: 'auto',
        }}
      />

      {/* Fallback raw JSON display if editor fails */}
      <details style={{ padding: '0.5rem', backgroundColor: '#f8f9fa' }}>
        <summary style={{ cursor: 'pointer', fontSize: '0.9rem', marginBottom: '0.5rem' }}>
          Raw JSON (fallback)
        </summary>
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
      </details>
    </div>
  );
};

export default JsonPayloadViewer;
