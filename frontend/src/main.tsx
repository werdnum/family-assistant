// Main entry point for the frontend build
// This file will be loaded by Vite and can import other modules

// Import our new globals CSS with Tailwind and shadcn/ui variables
import './styles/globals.css';

// Import our custom CSS
import './custom.css';

// Import vanilla-jsoneditor and make it globally available
import { createJSONEditor } from 'vanilla-jsoneditor';

// Log that the frontend is loaded (for development)
// console.log('Family Assistant frontend loaded');

// Export any utilities that might be needed globally
window.FamilyAssistant = {
  version: '0.1.0',
  loaded: true,
};

// Make vanilla-jsoneditor available globally for Jinja2 templates
window.createJSONEditor = createJSONEditor;
