import {
    createJSONEditor,
    createAjvValidator // Import validator function
} from 'https://unpkg.com/vanilla-jsoneditor/standalone.js';

document.addEventListener('DOMContentLoaded', () => {
    const testForms = document.querySelectorAll('.tool-test-form');

    // Function to initialize the arguments editor
    function initializeArgumentsEditor(containerId, schema) {
        const target = document.getElementById(containerId);
        if (!target) return null;

        // Create AJV validator using the schema
        const validator = createAjvValidator({ schema });

        return new createJSONEditor({
            target: target,
            props: { content: { json: {} }, validator: validator, mode: 'tree' } // Start empty, use tree mode, pass validator
        });
    }

    testForms.forEach(form => {
        const toolName = form.dataset.toolName;
        const argsTextarea = form.querySelector('textarea[name="arguments_json"]');        */

        const toolName = form.dataset.toolName;
        const editorContainerId = `args-editor-${form.querySelector('button').id.split('-').pop() || form.closest('.tool-card').id.split('-').pop() || Math.random().toString(36).substring(7)}`; // Derive index or use random for ID
        const argsEditorContainer = form.querySelector(`#${editorContainerId.startsWith('args-editor-') ? editorContainerId : `args-editor-${editorContainerId}`}`); // Find the editor container dynamically
        const schemaScript = form.querySelector(`#schema-${editorContainerId.split('-').pop()}`); // Find schema script tag
        const resultContainer = form.nextElementSibling; // Assuming result div is immediately after the form
        const submitButton = form.querySelector('button[type="submit"]');

        if (!toolName || !argsEditorContainer || !schemaScript || !resultContainer || !submitButton) {
            console.error('Form elements not found for tool test setup:', toolName);
            if (resultContainer) resultContainer.innerHTML = '<p class="error">Error: Form elements missing for testing.</p>';
            return;
        }

        // Initialize the arguments editor for this tool
        let schema = {};
        try {
            schema = JSON.parse(schemaScript.textContent || '{}');
        } catch(e) {
            console.error(`Failed to parse schema JSON for tool ${toolName}:`, e);
            argsEditorContainer.innerHTML = '<p class="error">Failed to load schema.</p>';
        }
        const argsEditorInstance = initializeArgumentsEditor(argsEditorContainer.id, schema);

        form.addEventListener('submit', async (event) => {
            event.preventDefault(); // Prevent default form submission

            const toolName = form.dataset.toolName;
            const argsTextarea = form.querySelector('textarea[name="arguments_json"]');
            const resultContainer = form.nextElementSibling; // Assuming result div is immediately after the form
            const submitButton = form.querySelector('button[type="submit"]');
        */
            if (!argsEditorInstance) {
                resultContainer.innerHTML = '<p class="error">Arguments editor not initialized.</p>';
                return;
            }


            // Disable button and clear previous results
            submitButton.disabled = true;
            submitButton.textContent = 'Testing...';
            resultContainer.innerHTML = '<p>Running...</p>'; // Clear previous result/error

            // Get content from the JSON editor instance
            let argsJson;
            try {
                const content = argsEditorInstance.get(); // Get content from editor
                // Check if content is text or json and handle appropriately
                argsJson = (content && typeof content === 'object' && 'json' in content) ? content.json : JSON.parse(content.text || '{}');
            } catch (e) {
                 // Editor itself might return errors via validation, but catch parsing fallback just in case
                 resultContainer.innerHTML = `<p class="error">Could not get arguments from editor: ${e.message}</p>`;
                 submitButton.disabled = false;
                 submitButton.textContent = 'Test Tool';
                 return;
            }

            // --- API Call ---
             try {
                 const response = await fetch(`/api/tools/execute/${encodeURIComponent(toolName)}`, {
                     method: 'POST',
                     headers: {
                         'Content-Type': 'application/json',
                         'Accept': 'application/json'
                     },
                     body: JSON.stringify({ arguments: argsJson })
                 });

                // Clear the "Running..." message before adding the editor
                resultContainer.innerHTML = '';

                // Display result using vanilla-jsoneditor
                new createJSONEditor({
                    target: resultContainer,
                    props: {
                        content: { json: resultData },
                        readOnly: true,
                        mainMenuBar: false,
                        navigationBar: false,
                        statusBar: false,
                        mode: 'tree'
                    }
                });

                if (!response.ok) {
                    console.error(`API Error (${response.status}):`, resultData);
                    // Optionally add an error class or message above the JSON editor
                     resultContainer.insertAdjacentHTML('beforebegin', `<p class="error">API Error (${response.status}): ${resultData.detail || 'Unknown error'}</p>`);
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
