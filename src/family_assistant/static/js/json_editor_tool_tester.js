// Import the result display editor (needs to be module type)
import { createJSONEditor as createVanillaEditor } from 'https://unpkg.com/vanilla-jsoneditor/standalone.js';

// Note: JSONEditor (for input) is loaded globally via the script tag in the HTML

document.addEventListener('DOMContentLoaded', () => {
    const testForms = document.querySelectorAll('.tool-test-form');

    testForms.forEach(form => {
        const toolName = form.dataset.toolName;
        const loopIndexMatch = form.querySelector('button[type="submit"]')?.id?.match(/\d+$/);
        const loopIndex = loopIndexMatch ? loopIndexMatch[0] : null;

        if (!loopIndex) {
            console.error(`Could not determine loop index for tool: ${toolName}`);
            return;
        }

        const editorContainerId = `args-editor-${loopIndex}`;
        const schemaScriptId = `schema-${loopIndex}`;
        const resultContainerId = `result-${loopIndex}`;
        const submitButtonId = `submit-button-${loopIndex}`;

        const editorContainer = document.getElementById(editorContainerId);
        const schemaScript = document.getElementById(schemaScriptId);
        const resultContainer = document.getElementById(resultContainerId);
        const submitButton = document.getElementById(submitButtonId);

        if (!toolName || !editorContainer || !schemaScript || !resultContainer || !submitButton) {
            console.error('Form elements not found for tool test setup:', toolName);
            if (resultContainer) resultContainer.innerHTML = '<p class="error">Error: Form elements missing for testing.</p>';
            return;
        }

        let schema = {};
        try {
            schema = JSON.parse(schemaScript.textContent || '{}');
            // Ensure schema is an object, handle null/empty cases json-editor might dislike
            if (schema === null || typeof schema !== 'object') {
                 schema = { type: "object", properties: {} }; // Default empty object schema
                 console.warn(`Schema for ${toolName} was null or not an object, using default empty schema.`);
            } else if (!schema.type) {
                 // json-editor often expects a top-level type
                 if (schema.properties && typeof schema.properties === 'object') {
                     schema.type = "object";
                 } else {
                     // Attempt a reasonable default if properties are missing/invalid
                     schema = { type: "object", properties: {} };
                     console.warn(`Schema for ${toolName} lacks a top-level 'type', using default empty object schema.`);
                 }
            }
        } catch (e) {
            console.error(`Failed to parse schema JSON for tool ${toolName}:`, e);
            editorContainer.innerHTML = '<p class="error">Failed to load schema.</p>';
            return; // Don't initialize editor if schema fails
        }

        // Check if the global JSONEditor (from @json-editor/json-editor) is loaded
        if (typeof JSONEditor === 'undefined') {
             console.error(`JSONEditor library not loaded for tool ${toolName}. Check script loading order in HTML.`);
             editorContainer.innerHTML = '<p class="error">Error: Input editor library failed to load.</p>';
             return; // Stop initialization for this form
        }

        // Initialize JSON Editor (for input)
        // Use html theme as it requires no external CSS framework
        const argsEditorInstance = new JSONEditor(editorContainer, {
            schema: schema,
            theme: 'bootstrap4', // Use basic HTML theme
            iconlib: null, // No icons needed
            disable_edit_json: true,
            disable_properties: true,
            disable_collapse: true,
            remove_button_labels: true, // Keep UI clean
            no_additional_properties: !schema.additionalProperties // Respect schema setting or default to false
        });

        // Handle form submission
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            submitButton.disabled = true;
            submitButton.textContent = 'Testing...';
            resultContainer.innerHTML = '<p>Running...</p>'; // Clear previous results

            // Validate and get arguments
            const validationErrors = argsEditorInstance.validate();
            if (validationErrors.length > 0) {
                resultContainer.innerHTML = `<p class="error">Invalid arguments:</p><pre>${JSON.stringify(validationErrors, null, 2)}</pre>`;
                submitButton.disabled = false;
                submitButton.textContent = 'Test Tool';
                return;
            }
            const argsJson = argsEditorInstance.getValue();

            // API Call
            let resultData;
            try {
                const response = await fetch(`/api/tools/execute/${encodeURIComponent(toolName)}`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    body: JSON.stringify({ arguments: argsJson })
                });
                resultData = await response.json();
                resultContainer.innerHTML = ''; // Clear "Running..."
                // Display result using vanilla-jsoneditor
                new createVanillaEditor({ target: resultContainer, props: { content: { json: resultData }, readOnly: true, mainMenuBar: false, navigationBar: false, statusBar: false, mode: 'tree' }});
                if (!response.ok) {
                    console.error(`API Error (${response.status}):`, resultData);
                    resultContainer.insertAdjacentHTML('afterbegin', `<p class="error">API Error (${response.status}): ${resultData.detail || 'Unknown error'}</p>`);
                }
            } catch (error) {
                console.error('Fetch Error:', error);
                resultContainer.innerHTML = `<p class="error">Network or Server Error: ${error.message}</p>`;
            } finally {
                submitButton.disabled = false;
                submitButton.textContent = 'Test Tool';
            }
        });
    });
});
