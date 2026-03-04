import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';
import { defineConfig } from 'vitest/config';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.js',
    css: true,
    testTimeout: 10000, // 10s per test (default 5s was too short)
    hookTimeout: 10000, // 10s for setup/teardown hooks
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      exclude: ['node_modules/', 'src/test/', '**/*.test.jsx', '**/*.test.js'],
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // Patch tapClientLookup to be error-tolerant in tests.
      // Works around assistant-ui/assistant-ui#3395 race condition.
      '@assistant-ui/store/dist/tapClientLookup.js': path.resolve(
        __dirname,
        './src/test/patches/tapClientLookup.js'
      ),
    },
  },
});
