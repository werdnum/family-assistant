import { createJSONEditor } from 'https://unpkg.com/vanilla-jsoneditor/standalone.js';

document.addEventListener('DOMContentLoaded', () => {
    const testForms = document.querySelectorAll('.tool-test-form');

    testForms.forEach(form => {
        form.addEventListener('submit', async (event) => {
            event.preventDefault(); // Prevent default form submission

            const toolName = form.dataset.toolName;
            const argsTextarea = form.querySelector('textarea[name="arguments_json"]');
            const resultContainer = form.nextElementSibling; // Assuming result div is immediately after the form
            const submitButton = form.querySelector('button[type="submit"]');

            if (!toolName || !argsTextarea || !resultContainer || !submitButton) {
                console.error('Form elements not found for tool:', toolName);
                resultContainer.innerHTML = '<p class="error">Error: Form elements missing.</p>';
                return;
            }

            // Disable button and clear previous results
            submitButton.disabled = true;
            submitButton.textContent = 'Testing...';
            resultContainer.innerHTML = '<p>Running...</p>'; // Clear previous result/error

            let argsJson;
            try {
                argsJson = JSON.parse(argsTextarea.value || '{}'); // Default to empty object if textarea is empty
            } catch (e) {
                resultContainer.innerHTML = `<p class="error">Invalid JSON arguments: ${e.message}</p>`;
                submitButton.disabled = false;
                submitButton.textContent = 'Test Tool';
                return;
            }

            try {
                const response = await fetch(`/api/tools/execute/${encodeURIComponent(toolName)}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    body: JSON.stringify({ arguments: argsJson })
                });

                const resultData = await response.json();

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
