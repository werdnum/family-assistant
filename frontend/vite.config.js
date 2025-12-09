import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';
import { defineConfig } from 'vite';
import { VitePWA } from 'vite-plugin-pwa';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => ({
  // Set base URL - root for dev, /static/dist/ for production
  base: mode === 'development' ? '/' : '/static/dist/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  optimizeDeps: {
    // Force Vite to pre-bundle these CommonJS dependencies for ESM compatibility
    include: ['vanilla-jsoneditor'],
  },
  plugins: [
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: null, // Manual SW registration (see router-entry.jsx)
      strategies: 'injectManifest', // Use custom service worker
      srcDir: 'src', // Source directory containing sw.js
      filename: 'sw.js', // Custom service worker filename
      manifestFilename: 'manifest.webmanifest', // Manifest filename
      includeAssets: [
        'favicon.ico',
        'apple-touch-icon.png',
        'pwa-192x192.png',
        'pwa-512x512.png',
        'badge.png',
      ],
      manifest: {
        name: 'Family Assistant',
        short_name: 'FamAssist',
        description: 'Family Assistant PWA',
        start_url: '/chat',
        scope: '/',
        theme_color: '#ffffff',
        icons: [
          {
            src: 'pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: 'pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
          },
          {
            src: 'apple-touch-icon.png',
            sizes: '180x180',
            type: 'image/png',
          },
          {
            src: 'badge.png',
            sizes: '96x96',
            type: 'image/png',
          },
        ],
      },
      injectManifest: {
        // Workbox configuration for manifest injection
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        globIgnores: ['**/sw.js'], // Don't precache the SW itself
      },
      devOptions: {
        enabled: true,
      },
    }),
    react(),
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
          }
          // Everything else goes to React Router
          else if (
            !url.pathname.startsWith('/@vite') &&
            !url.pathname.startsWith('/@react-refresh') &&
            !url.pathname.startsWith('/src') &&
            !url.pathname.startsWith('/node_modules') &&
            !url.pathname.includes('.html') &&
            // Skip files with extensions (like .png, .jpg, .svg, .json, etc.)
            !url.pathname.match(/\.[a-zA-Z0-9-]+$/)
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
    // Increase chunk size warning limit since we're code splitting
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      // Define entry points including HTML files
      input: {
        main: path.resolve(__dirname, 'index.html'),
        chat: path.resolve(__dirname, 'chat.html'),
        router: path.resolve(__dirname, 'router.html'),
        'tool-test-bench': path.resolve(__dirname, 'tool-test-bench.html'),
      },
      output: {
        // Manual chunks configuration to optimize loading of page-specific deps
        manualChunks: (id) => {
          if (id.includes('node_modules')) {
            // CRITICAL: React and core UI libraries MUST stay in the entry bundle.
            // Moving React to a separate chunk causes race conditions where the app
            // tries to render before React is loaded, breaking all page initialization.
            if (
              id.includes('/react/') ||
              id.includes('react-dom') ||
              id.includes('react-router') ||
              id.includes('scheduler') ||
              id.includes('prop-types') ||
              id.includes('@restart/hooks') ||
              id.includes('use-')
            ) {
              return undefined; // Keep in entry bundle
            }

            // Core UI libraries - used on almost every page, keep in entry
            if (
              id.includes('@radix-ui') ||
              id.includes('lucide-react') ||
              id.includes('class-variance-authority') ||
              id.includes('clsx') ||
              id.includes('tailwind-merge')
            ) {
              return undefined; // Keep in entry bundle
            }

            // === Page-specific dependencies - safe to split ===

            // Chat-specific UI components (~96KB, only needed on chat page)
            if (id.includes('@assistant-ui')) {
              return 'assistant-ui';
            }

            // Syntax highlighter - very large (~1.7MB), only for code blocks
            if (id.includes('react-syntax-highlighter')) {
              return 'syntax-highlighter';
            }

            // Markdown processing (~162KB, only for rendering markdown content)
            if (
              id.includes('react-markdown') ||
              id.includes('remark') ||
              id.includes('rehype') ||
              id.includes('unified') ||
              id.includes('micromark') ||
              id.includes('mdast') ||
              id.includes('vfile') ||
              id.includes('unist')
            ) {
              return 'markdown';
            }

            // JSON editor - Modern vanilla-jsoneditor (~1.1MB, Tools page only)
            // Note: Requires optimizeDeps.include for proper ESM pre-bundling
            if (
              id.includes('vanilla-jsoneditor') ||
              id.includes('svelte') ||
              id.includes('@codemirror') ||
              id.includes('vanilla-picker') ||
              id.includes('jsonrepair') ||
              id.includes('immutable-json-patch') ||
              id.includes('jmespath') ||
              id.includes('jsonpath-plus')
            ) {
              return 'vanilla-jsoneditor';
            }

            // JSON editor - Classic @json-editor (~536KB, different tool UI)
            if (id.includes('@json-editor') || id.includes('json-editor/')) {
              return 'json-editor-classic';
            }

            // Let other dependencies stay with the importing chunk
            return undefined;
          }
          // Keep app code in the default chunks
        },
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
      // API endpoints (including WebSocket for /api/asterisk/live)
      '/api': {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || 8000}`,
        changeOrigin: true,
        ws: true, // Enable WebSocket proxying
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
