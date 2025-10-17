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
      strategies: 'injectManifest', // Use custom service worker
      srcDir: 'src', // Source directory containing sw.js
      filename: 'sw.js', // Custom service worker filename
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
        // Manual chunks configuration to avoid loading page-specific deps globally
        manualChunks: (id) => {
          // Let Rollup create chunks for node_modules when needed
          if (id.includes('node_modules')) {
            // DON'T manually chunk React - let it be included in the entry bundle
            // This ensures React is always available when the app starts
            if (
              id.includes('react') ||
              id.includes('react-dom') ||
              id.includes('react-router') ||
              id.includes('scheduler') ||
              id.includes('@restart/hooks') || // React hooks utilities
              id.includes('use-') // Common React hook libraries
            ) {
              // Return undefined to include in entry chunk
              return undefined;
            }

            // Only split out truly optional/page-specific dependencies:

            // Markdown libraries (only needed for markdown rendering)
            if (
              id.includes('react-markdown') ||
              id.includes('remark') ||
              id.includes('rehype') ||
              id.includes('unified') ||
              id.includes('micromark') ||
              id.includes('mdast')
            ) {
              return 'markdown';
            }

            // JSON editor - DON'T split it out, let it be bundled where it's used
            // This avoids CommonJS/ESM issues with vanilla-jsoneditor
            if (id.includes('json-editor') || id.includes('vanilla-jsoneditor')) {
              // Return undefined to keep it with the importing module
              return undefined;
            }

            // Icon libraries - keep them with importing modules
            if (id.includes('lucide-react')) {
              return undefined;
            }

            // Chat-specific UI components
            if (id.includes('@assistant-ui')) {
              return 'assistant-ui';
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
