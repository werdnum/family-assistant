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
      targets: ['defaults', 'not IE 11'],
    }),
    // Custom plugin to handle clean URLs (e.g., /chat -> /chat.html)
    {
      name: 'html-fallback',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          // Rewrite specific routes to their appropriate HTML files
          // Handle query parameters properly
          const url = new URL(req.url, 'http://localhost');

          // React Router routes - use router.html
          if (
            url.pathname === '/chat' ||
            url.pathname === '/context' ||
            url.pathname === '/notes' ||
            url.pathname.startsWith('/notes/') ||
            url.pathname === '/tasks' ||
            url.pathname === '/event-listeners' ||
            url.pathname.startsWith('/event-listeners/') ||
            url.pathname === '/history' ||
            url.pathname.startsWith('/history/')
          ) {
            req.url = '/router.html' + url.search;
          }
          // Individual app routes - use specific HTML files
          else if (url.pathname === '/tools') {
            req.url = '/tools.html' + url.search;
          } else if (url.pathname === '/tool-test-bench') {
            req.url = '/tool-test-bench.html' + url.search;
          } else if (url.pathname === '/errors' || url.pathname.startsWith('/errors/')) {
            req.url = '/errors.html' + url.search;
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
        tools: path.resolve(__dirname, 'tools.html'),
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
    // Allow specific hosts for development access
    allowedHosts: [
      'localhost',
      'grotten.home.andrewandteija.com',
      'family-assistant-dev.andrewgarrett.dev',
    ],
    // Proxy all non-asset requests to our FastAPI backend
    proxy: {
      // Proxy everything except Vite's own paths, static assets, and HTML entry points
      '^(?!/@vite|/@react-refresh|/src|/node_modules|/__vite_ping|/index\.html|/chat\.html|/chat$|/router\.html|/context$|/tools\.html|/tools$|/tool-test-bench\.html|/tool-test-bench$|/errors\.html|/errors$|/errors/|/notes\.html|/notes$|/notes/|/tasks\.html|/tasks$).*':
        {
          target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
          changeOrigin: true,
        },
    },
  },
}));
