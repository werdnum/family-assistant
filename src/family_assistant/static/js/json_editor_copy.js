/**
 * Adds a copy button to a vanilla-jsoneditor instance
 * @param {HTMLElement} container - The container element of the JSON editor
 * @param {Object|Array|null} jsonData - The JSON data to copy
 * @param {string} buttonText - Text for the copy button (default: "Copy JSON")
 */
export function addCopyButton(container, jsonData, buttonText = "Copy JSON") {
    if (!container || jsonData === null || jsonData === undefined) {
        return;
    }

    // Create wrapper div to hold editor and button
    const wrapper = document.createElement('div');
    wrapper.style.position = 'relative';
    
    // Move all children from container to wrapper
    while (container.firstChild) {
        wrapper.appendChild(container.firstChild);
    }
    
    // Create copy button
    const copyButton = document.createElement('button');
    copyButton.textContent = buttonText;
    copyButton.type = 'button';
    copyButton.className = 'json-copy-button';
    copyButton.style.cssText = `
        position: absolute;
        top: 5px;
        right: 5px;
        padding: 5px 10px;
        font-size: 12px;
        background-color: #f0f0f0;
        border: 1px solid #ccc;
        border-radius: 3px;
        cursor: pointer;
        z-index: 10;
        transition: background-color 0.2s;
    `;
    
    // Add hover effect
    copyButton.addEventListener('mouseenter', () => {
        copyButton.style.backgroundColor = '#e0e0e0';
    });
    
    copyButton.addEventListener('mouseleave', () => {
        copyButton.style.backgroundColor = '#f0f0f0';
    });
    
    // Add click handler
    copyButton.addEventListener('click', async () => {
        try {
            const jsonString = JSON.stringify(jsonData, null, 2);
            await navigator.clipboard.writeText(jsonString);
            
            // Show success feedback
            const originalText = copyButton.textContent;
            copyButton.textContent = 'Copied!';
            copyButton.style.backgroundColor = '#4CAF50';
            copyButton.style.color = 'white';
            copyButton.style.borderColor = '#45a049';
            
            setTimeout(() => {
                copyButton.textContent = originalText;
                copyButton.style.backgroundColor = '#f0f0f0';
                copyButton.style.color = '';
                copyButton.style.borderColor = '#ccc';
            }, 2000);
        } catch (err) {
            console.error('Failed to copy JSON:', err);
            // Show error feedback
            const originalText = copyButton.textContent;
            copyButton.textContent = 'Failed!';
            copyButton.style.backgroundColor = '#f44336';
            copyButton.style.color = 'white';
            copyButton.style.borderColor = '#da190b';
            
            setTimeout(() => {
                copyButton.textContent = originalText;
                copyButton.style.backgroundColor = '#f0f0f0';
                copyButton.style.color = '';
                copyButton.style.borderColor = '#ccc';
            }, 2000);
        }
    });
    
    // Add wrapper and button to container
    wrapper.appendChild(copyButton);
    container.appendChild(wrapper);
}

/**
 * Enhanced version of createJSONEditor that automatically adds a copy button
 * @param {Object} options - Options for createJSONEditor
 * @param {Object|Array|null} jsonData - The JSON data being displayed
 * @param {string} copyButtonText - Text for the copy button
 * @returns {Object} The created editor instance
 */
export function createJSONEditorWithCopy(createJSONEditor, options, jsonData, copyButtonText = "Copy JSON") {
    const editor = new createJSONEditor(options);
    
    // Wait a bit for the editor to render, then add the copy button
    setTimeout(() => {
        addCopyButton(options.target, jsonData, copyButtonText);
    }, 100);
    
    return editor;
}