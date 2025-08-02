import React, { useState, useEffect, useRef } from 'react';
import { JSONEditor } from '@json-editor/json-editor';
import NavHeader from '../chat/NavHeader';

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

  const initializeJSONEditor = () => {
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

  // Execute selected tool
  const executeTool = async () => {
    if (!selectedTool || !editorInstanceRef.current) {
      return;
    }

    setExecutionLoading(true);
    setExecutionError(null);
    setExecutionResult(null);

    try {
      // Validate the form
      const validationErrors = editorInstanceRef.current.validate();
      if (validationErrors.length > 0) {
        setExecutionError(`Invalid arguments: ${JSON.stringify(validationErrors, null, 2)}`);
        setExecutionLoading(false);
        return;
      }

      // Get the arguments from the editor
      const args = editorInstanceRef.current.getValue();

      const response = await fetch(`/api/tools/execute/${selectedTool}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ arguments: args }),
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
    return (
      <div className="tools-container">
        <div className="tools-header">
          <h1>Tools</h1>
          <p>Loading available tools...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="tools-container">
        <div className="tools-header">
          <h1>Tools</h1>
          <p className="error">Error loading tools: {error}</p>
        </div>
      </div>
    );
  }

  const selectedToolDef = selectedTool
    ? tools.find((tool) => tool.function?.name === selectedTool)
    : null;

  return (
    <>
      <NavHeader currentPage="tools" />
      <div className="tools-container">
        <div className="tools-header">
          <h1>Tools</h1>
          <p>Execute and test available tools</p>
        </div>

        <div className="tools-content">
          <div className="tools-section">
            <h2>Available Tools ({tools.filter((tool) => tool.function?.name).length})</h2>
            <div className="tools-list">
              {tools
                .filter((tool) => tool.function?.name)
                .map((tool) => {
                  const toolName = tool.function.name;
                  return (
                    <button
                      key={toolName}
                      className={`tool-button ${selectedTool === toolName ? 'selected' : ''}`}
                      onClick={() => {
                        setSelectedTool(toolName);
                        setExecutionResult(null);
                        setExecutionError(null);
                      }}
                      title={tool.function?.description || ''}
                    >
                      {toolName}
                    </button>
                  );
                })}
            </div>
          </div>

          {selectedTool && selectedToolDef && (
            <div className="tool-execution-section">
              <h2>Execute Tool: {selectedTool}</h2>

              {selectedToolDef.function?.description && (
                <p className="tool-description">{selectedToolDef.function.description}</p>
              )}

              <div className="tool-form">
                <label>Parameters:</label>
                <div ref={jsonEditorRef} className="json-editor-container" />

                <button
                  onClick={executeTool}
                  disabled={executionLoading}
                  className="execute-button"
                >
                  {executionLoading ? 'Executing...' : 'Execute Tool'}
                </button>
              </div>

              {executionError && (
                <div className="execution-result error">
                  <h3>Execution Error</h3>
                  <pre>{executionError}</pre>
                </div>
              )}

              {executionResult && (
                <div className="execution-result success">
                  <h3>Execution Result</h3>
                  <pre>{JSON.stringify(executionResult, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
};

export default ToolsApp;
