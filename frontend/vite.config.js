import { defineConfig } from 'vite';
import legacy from '@vitejs/plugin-legacy';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // Set base URL for production assets
  base: '/static/dist/',
  plugins: [
    react(),
    legacy({
      targets: ['defaults', 'not IE 11']
    })
  ],
  build: {
    // Generate a manifest file to connect assets to Jinja2
    manifest: true,
    // Output assets to the existing static directory structure
    outDir: path.resolve(__dirname, '../src/family_assistant/static/dist'),
    // Empty the output directory on each build
    emptyOutDir: true,
    rollupOptions: {
      // Define entry points including HTML files
      input: {
        main: path.resolve(__dirname, 'index.html'),
        chat: path.resolve(__dirname, 'chat.html'),
      },
    },
  },
  server: {
    // Dev server port
    port: 5173,
    // Listen on all interfaces for remote access
    host: true,
    // Allow specific hosts for development access
    allowedHosts: ['localhost', 'grotten.home.andrewandteija.com', 'family-assistant-dev.andrewgarrett.dev'],
    // Proxy all non-asset requests to our FastAPI backend
    proxy: {
      // Proxy everything except Vite's own paths, static assets, and HTML entry points
      '^(?!/@vite|/@react-refresh|/src|/node_modules|/__vite_ping|/index\.html|/chat\.html).*': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      }
    },
  },
});
