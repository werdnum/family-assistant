import React, { useState, useEffect, useRef } from 'react';
import './tools.css';

// Global variable to cache the dynamic import promise
let jsonEditorImportPromise = null;

const ToolsApp = () => {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedTool, setSelectedTool] = useState(null);
  const [executionResult, setExecutionResult] = useState(null);
  const [executionLoading, setExecutionLoading] = useState(false);
  const [executionError, setExecutionError] = useState(null);
  const jsonEditorRef = useRef(null);
  const editorInstanceRef = useRef(null);
  const JSONEditorRef = useRef(null);

  // Set the page title
  useEffect(() => {
    document.title = 'Tools - Family Assistant';
  }, []);

  // Fetch available tools
  useEffect(() => {
    const fetchTools = async () => {
      try {
        const response = await fetch('/api/tools/definitions');
        if (!response.ok) {
          throw new Error(`Failed to fetch tools: ${response.status}`);
        }
        const data = await response.json();
        setTools(data.tools || []);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    fetchTools();
  }, []);

  const initializeJSONEditor = async () => {
    if (!selectedTool || !jsonEditorRef.current) {
      return;
    }

    // Clean up any existing editor
    if (editorInstanceRef.current) {
      editorInstanceRef.current.destroy();
      editorInstanceRef.current = null;
    }

    // Find the selected tool definition
    const toolDef = tools.find((tool) => tool.function?.name === selectedTool);
    if (!toolDef) {
      console.error('Tool definition not found for:', selectedTool);
      return;
    }

    let schema = toolDef.function?.parameters || {};

    // Ensure schema is properly formatted for JSON Editor
    if (!schema.type) {
      if (schema.properties && typeof schema.properties === 'object') {
        schema.type = 'object';
      } else {
        schema = { type: 'object', properties: {} };
      }
    }

    // Initialize the JSON Editor
    try {
      // Dynamically import JSONEditor if not already loaded
      if (!JSONEditorRef.current) {
        if (!jsonEditorImportPromise) {
          jsonEditorImportPromise = import('@json-editor/json-editor').catch((error) => {
            // Reset the promise cache on failure to allow retry
            jsonEditorImportPromise = null;
            throw error;
          });
        }
        const module = await jsonEditorImportPromise;
        JSONEditorRef.current = module.JSONEditor;
      }
      const JSONEditor = JSONEditorRef.current;

      editorInstanceRef.current = new JSONEditor(jsonEditorRef.current, {
        schema: schema,
        theme: 'html',
        iconlib: null,
        disable_edit_json: true,
        disable_properties: true,
        disable_collapse: true,
        remove_button_labels: true,
        no_additional_properties: !schema.additionalProperties,
      });
    } catch (err) {
      console.error('Failed to initialize JSON Editor:', err);
      jsonEditorRef.current.innerHTML = '<p class="error">Failed to load parameter editor</p>';
    }
  };

  // Initialize JSON Editor when a tool is selected
  useEffect(() => {
    if (selectedTool && jsonEditorRef.current) {
      initializeJSONEditor();
    }

    return () => {
      if (editorInstanceRef.current) {
        editorInstanceRef.current.destroy();
        editorInstanceRef.current = null;
      }
    };
  }, [selectedTool, tools]);

  const handleToolSelect = (toolName) => {
    setSelectedTool(toolName);
    setExecutionResult(null);
    setExecutionError(null);
  };

  const handleExecute = async () => {
    if (!selectedTool || !editorInstanceRef.current) {
      return;
    }

    setExecutionLoading(true);
    setExecutionError(null);
    setExecutionResult(null);

    try {
      // Validate the form
      const errors = editorInstanceRef.current.validate();
      if (errors.length > 0) {
        throw new Error('Please fix validation errors before executing');
      }

      // Get the form values
      const parameters = editorInstanceRef.current.getValue();

      // Execute the tool
      const response = await fetch('/api/tools/execute', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          tool_name: selectedTool,
          parameters: parameters,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || `Execution failed: ${response.status}`);
      }

      setExecutionResult(data);
    } catch (err) {
      setExecutionError(err.message);
    } finally {
      setExecutionLoading(false);
    }
  };

  if (loading) {
    return <div className="tools-loading">Loading tools...</div>;
  }

  if (error) {
    return <div className="tools-error">Error: {error}</div>;
  }

  return (
    <div className="tools-app">
      <div className="tools-header">
        <h1>Tool Explorer</h1>
        <p className="tools-description">
          Explore and test the available tools in the Family Assistant system
        </p>
      </div>

      <div className="tools-container">
        {/* Tool list sidebar */}
        <div className="tools-sidebar">
          <h2>Available Tools</h2>
          <div className="tools-list">
            {tools.map((tool) => (
              <button
                key={tool.function?.name || tool.name}
                className={`tool-item ${
                  selectedTool === (tool.function?.name || tool.name) ? 'selected' : ''
                }`}
                onClick={() => handleToolSelect(tool.function?.name || tool.name)}
              >
                <span className="tool-name">{tool.function?.name || tool.name}</span>
                {tool.function?.description && (
                  <span className="tool-description">{tool.function?.description}</span>
                )}
              </button>
            ))}
          </div>
        </div>

        {/* Tool details and execution */}
        <div className="tools-main">
          {selectedTool ? (
            <>
              <div className="tool-details">
                <h2>{selectedTool}</h2>
                {tools.find((t) => (t.function?.name || t.name) === selectedTool)?.function
                  ?.description && (
                  <p className="tool-full-description">
                    {
                      tools.find((t) => (t.function?.name || t.name) === selectedTool)?.function
                        ?.description
                    }
                  </p>
                )}

                <div className="tool-parameters">
                  <h3>Parameters</h3>
                  <div ref={jsonEditorRef} className="json-editor-container" />
                </div>

                <div className="tool-actions">
                  <button
                    onClick={handleExecute}
                    disabled={executionLoading}
                    className="btn-execute"
                  >
                    {executionLoading ? 'Executing...' : 'Execute Tool'}
                  </button>
                </div>
              </div>

              {/* Execution results */}
              {(executionResult || executionError) && (
                <div className="execution-results">
                  <h3>Execution Results</h3>
                  {executionError && (
                    <div className="execution-error">
                      <strong>Error:</strong> {executionError}
                    </div>
                  )}
                  {executionResult && (
                    <div className="execution-success">
                      {executionResult.success ? (
                        <>
                          <div className="result-status success">✓ Success</div>
                          {executionResult.result && (
                            <div className="result-content">
                              <strong>Result:</strong>
                              <pre>{JSON.stringify(executionResult.result, null, 2)}</pre>
                            </div>
                          )}
                        </>
                      ) : (
                        <>
                          <div className="result-status failure">✗ Failed</div>
                          {executionResult.error && (
                            <div className="result-content">
                              <strong>Error:</strong> {executionResult.error}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
            </>
          ) : (
            <div className="no-tool-selected">
              <p>Select a tool from the list to view details and test it</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default ToolsApp;
