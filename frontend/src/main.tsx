// Main entry point for the frontend build
// This file will be loaded by Vite and can import other modules

// Import our new globals CSS with Tailwind and shadcn/ui variables
import './styles/globals.css';

// Import our custom CSS
import './custom.css';

// Initialize global error handlers early to capture any errors during initialization
import { initializeErrorHandlers } from './errors/errorHandlers';
initializeErrorHandlers();

// Log that the frontend is loaded (for development)
// console.log('Family Assistant frontend loaded');

// Export any utilities that might be needed globally
// @ts-expect-error - partial assignment, full interface set in chat/index.jsx
window.FamilyAssistant = {
  version: '0.1.0',
  loaded: true,
};
