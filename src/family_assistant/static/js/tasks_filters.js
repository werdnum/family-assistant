document.addEventListener('DOMContentLoaded', function() {
    const filterForm = document.getElementById('filter-form');
    const dateFromInput = document.getElementById('date-from-filter');
    const dateToInput = document.getElementById('date-to-filter');
    
    // Convert UTC datetime string to local datetime-local format
    function utcToLocal(utcString) {
        if (!utcString) return '';
        const date = new Date(utcString);
        // Format as YYYY-MM-DDTHH:MM for datetime-local input
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        return `${year}-${month}-${day}T${hours}:${minutes}`;
    }
    
    // Convert local datetime-local format to UTC for submission
    function localToUtc(localString) {
        if (!localString) return '';
        const date = new Date(localString);
        return date.toISOString();
    }
    
    // Initialize date inputs with converted values
    const urlParams = new URLSearchParams(window.location.search);
    const dateFromParam = urlParams.get('date_from');
    const dateToParam = urlParams.get('date_to');
    
    if (dateFromParam) {
        dateFromInput.value = utcToLocal(dateFromParam);
    }
    if (dateToParam) {
        dateToInput.value = utcToLocal(dateToParam);
    }
    
    // Handle form submission
    filterForm.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const formData = new FormData(filterForm);
        const params = new URLSearchParams();
        
        // Process each form field
        for (const [key, value] of formData.entries()) {
            if (value) {
                if (key === 'date_from' || key === 'date_to') {
                    // Convert local datetime to UTC
                    params.set(key, localToUtc(value));
                } else {
                    params.set(key, value);
                }
            }
        }
        
        // Redirect with new parameters
        window.location.href = `${filterForm.action}?${params.toString()}`;
    });
    
    // Add loading indicator during page transitions
    window.addEventListener('beforeunload', function() {
        filterForm.classList.add('loading');
    });
});