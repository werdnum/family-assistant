document.addEventListener('DOMContentLoaded', function () {
    const createTokenForm = document.getElementById('createTokenForm');
    const newTokenSection = document.getElementById('newTokenSection');
    const newTokenValueInput = document.getElementById('newTokenValue');
    const copyTokenButton = document.getElementById('copyTokenButton');
    const copyMessage = document.getElementById('copyMessage');
    const errorMessageDiv = document.getElementById('errorMessage');
    
    // Retrieve server_url from a data attribute on a DOM element
    // e.g., set on the main container div in the HTML template
    const containerDiv = document.querySelector('.container.mt-4'); // Or a more specific ID/class
    const serverUrl = containerDiv ? containerDiv.dataset.serverUrl : '';

    if (!serverUrl) {
        console.error("Server URL not found in data attribute. API calls will fail.");
        errorMessageDiv.textContent = 'Configuration error: Server URL not found. Cannot create tokens.';
        errorMessageDiv.style.display = 'block';
        if (createTokenForm) createTokenForm.querySelector('button[type="submit"]').disabled = true;
    }

    if (createTokenForm) {
        createTokenForm.addEventListener('submit', async function (event) {
            event.preventDefault();
            newTokenSection.style.display = 'none';
            errorMessageDiv.style.display = 'none';
            errorMessageDiv.textContent = '';
            copyMessage.style.display = 'none';

            const tokenName = document.getElementById('tokenName').value;
            const tokenExpiresAtInput = document.getElementById('tokenExpiresAt').value;
            
            let expiresAtISO = null;
            if (tokenExpiresAtInput) {
                try {
                    const localDate = new Date(tokenExpiresAtInput);
                    if (isNaN(localDate.getTime())) {
                        throw new Error("Invalid date format");
                    }
                    expiresAtISO = localDate.toISOString();
                } catch (e) {
                    errorMessageDiv.textContent = 'Invalid date format for "Expires At". Please use a valid date and time.';
                    errorMessageDiv.style.display = 'block';
                    return;
                }
            }

            const payload = {
                name: tokenName,
                expires_at: expiresAtISO
            };

            try {
                const response = await fetch(`${serverUrl}/api/me/tokens`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(payload)
                });

                if (response.ok) {
                    const result = await response.json();
                    newTokenValueInput.value = result.full_token;
                    newTokenSection.style.display = 'block';
                    createTokenForm.reset(); 
                    setTimeout(() => {
                        window.location.reload();
                    }, 15000); 
                } else {
                    const errorData = await response.json();
                    errorMessageDiv.textContent = `Error creating token: ${errorData.detail || response.statusText}`;
                    errorMessageDiv.style.display = 'block';
                }
            } catch (error) {
                console.error('Error submitting form:', error);
                errorMessageDiv.textContent = 'An unexpected error occurred. Please try again.';
                errorMessageDiv.style.display = 'block';
            }
        });
    }

    if (copyTokenButton) {
        copyTokenButton.addEventListener('click', function () {
            newTokenValueInput.select();
            newTokenValueInput.setSelectionRange(0, 99999); 

            try {
                document.execCommand('copy');
                copyMessage.style.display = 'inline';
                setTimeout(() => {
                    copyMessage.style.display = 'none';
                }, 2000);
            } catch (err) {
                console.error('Failed to copy token:', err);
                alert('Failed to copy token. Please copy it manually.');
            }
        });
    }
});
