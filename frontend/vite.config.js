import { defineConfig } from 'vite';
import legacy from '@vitejs/plugin-legacy';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => ({
  // Set base URL - root for dev, /static/dist/ for production
  base: mode === 'development' ? '/' : '/static/dist/',
  plugins: [
    react(),
    legacy({
      targets: ['defaults', 'not IE 11']
    }),
    // Custom plugin to handle clean URLs (e.g., /chat -> /chat.html)
    {
      name: 'html-fallback',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          // Rewrite /chat to /chat.html (and potentially other routes in the future)
          // Handle query parameters properly
          const url = new URL(req.url, 'http://localhost');
          if (url.pathname === '/chat') {
            req.url = '/chat.html' + url.search;
          }
          next();
        });
      }
    }
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
    allowedHosts: ['localhost', 'grotten.home.alexandtaylor.com', 'family-assistant-dev.alexsmith.dev'],
    // Proxy all non-asset requests to our FastAPI backend
    proxy: {
      // Proxy everything except Vite's own paths, static assets, and HTML entry points
      '^(?!/@vite|/@react-refresh|/src|/node_modules|/__vite_ping|/index\.html|/chat\.html|/chat$).*': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      }
    },
  },
}));
