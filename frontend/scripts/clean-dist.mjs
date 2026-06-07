import { mkdirSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(scriptDir, '../../src/family_assistant/static/dist');

rmSync(distDir, {
  force: true,
  maxRetries: 5,
  recursive: true,
  retryDelay: 200,
});
mkdirSync(distDir, { recursive: true });
