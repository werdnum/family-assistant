import legacy from '@vitejs/plugin-legacy';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';
import { defineConfig } from 'vite';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => ({
  // Set base URL - root for dev, /static/dist/ for production
  base: mode === 'development' ? '/' : '/static/dist/',
  plugins: [
    react(),
    legacy({
      targets: ['defaults', 'not IE 11'],
    }),
    // Custom plugin to handle clean URLs (e.g., /chat -> /chat.html)
    {
      name: 'html-fallback',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          // Handle query parameters properly
          const url = new URL(req.url, 'http://localhost');

          // Skip API routes and specific backend pages - let them proxy through
          if (
            url.pathname.startsWith('/api/') ||
            url.pathname.startsWith('/webhook/') ||
            url.pathname === '/auth/login' ||
            url.pathname === '/auth/logout' ||
            url.pathname === '/auth/callback' ||
            url.pathname === '/documents' ||
            url.pathname === '/vector-search' ||
            url.pathname.startsWith('/static/') ||
            url.pathname === '/favicon.ico'
          ) {
            // Let these pass through to the proxy
            next();
            return;
          }

          // Special standalone React apps (not using React Router)
          if (url.pathname === '/tool-test-bench') {
            req.url = '/tool-test-bench.html' + url.search;
          } else if (url.pathname === '/errors' || url.pathname.startsWith('/errors/')) {
            req.url = '/errors.html' + url.search;
          }
          // Everything else goes to React Router
          else if (
            !url.pathname.startsWith('/@vite') &&
            !url.pathname.startsWith('/@react-refresh') &&
            !url.pathname.startsWith('/src') &&
            !url.pathname.startsWith('/node_modules') &&
            !url.pathname.includes('.html')
          ) {
            req.url = '/router.html' + url.search;
          }

          next();
        });
      },
    },
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
        router: path.resolve(__dirname, 'router.html'),
        'tool-test-bench': path.resolve(__dirname, 'tool-test-bench.html'),
        errors: path.resolve(__dirname, 'errors.html'),
      },
    },
  },
  server: {
    // Dev server port
    port: 5173,
    // Listen on all interfaces for remote access
    host: true,
    // Allow all hosts (set to array of specific hosts to restrict access)
    allowedHosts: true,
    // Proxy API and backend-only routes to FastAPI
    proxy: {
      // API endpoints
      '/api': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      // Webhooks
      '/webhook': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      // Auth endpoints
      '/auth': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      // Remaining Jinja2 pages
      '/documents': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      '/vector-search': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      // Static files from backend
      '/static': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
      // Favicon
      '/favicon.ico': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
      },
    },
  },
}));
