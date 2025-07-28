// Main entry point for the frontend build
// This file will be loaded by Vite and can import other modules

// Import Simple.css locally instead of from CDN
import 'simpledotcss/simple.css';

// Import our custom CSS
import './custom.css';

// Log that the frontend is loaded (for development)
// console.log('Family Assistant frontend loaded');

// Export any utilities that might be needed globally
window.FamilyAssistant = {
  version: '0.1.0',
  loaded: true,
};
