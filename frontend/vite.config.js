import { defineConfig } from 'vite';
import legacy from '@vitejs/plugin-legacy';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [
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
      // Define entry points for our JS
      input: {
        main: path.resolve(__dirname, 'src/main.js'),
      },
    },
  },
  server: {
    // Dev server port
    port: 5173,
    // Listen on all interfaces for remote access
    host: true,
    // Allow specific hosts for development access
    allowedHosts: ['localhost', 'grotten.home.andrewandteija.com'],
    // Proxy all non-asset requests to our FastAPI backend
    proxy: {
      // Proxy everything except Vite's own paths and static assets
      '^(?!/@vite|/src|/node_modules|/__vite_ping).*': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      }
    },
  },
});