import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{js,jsx,ts,tsx}'],
    languageOptions: {
      globals: {
        // Browser globals
        window: 'readonly',
        document: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        crypto: 'readonly',
        localStorage: 'readonly',
        URLSearchParams: 'readonly',
        AbortController: 'readonly',
        TextDecoder: 'readonly',
        FileReader: 'readonly',
        File: 'readonly',
        setTimeout: 'readonly',
        global: 'readonly',
        // React globals (for JSX)
        React: 'readonly',
        // Test globals (Vitest)
        describe: 'readonly',
        test: 'readonly',
        expect: 'readonly',
        beforeEach: 'readonly',
        afterEach: 'readonly',
        beforeAll: 'readonly',
        afterAll: 'readonly',
        vi: 'readonly',
      },
      parserOptions: {
        project: false, // Use non-type-aware mode for now
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    rules: {
      // Disable formatting rules - let Biome handle these
      'indent': 'off',
      'quotes': 'off',
      'semi': 'off',
      'comma-dangle': 'off',
      'arrow-parens': 'off',
      'space-before-function-paren': 'off',
      'object-curly-spacing': 'off',
      'array-bracket-spacing': 'off',
      'max-len': 'off',

      // React-specific rules (basic without plugin for now)
      'react/jsx-uses-react': 'off',
      'react/react-in-jsx-scope': 'off',

      // TypeScript-specific adjustments
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      }],
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/explicit-function-return-type': 'off',
      '@typescript-eslint/explicit-module-boundary-types': 'off',

      // General best practices
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      'no-debugger': 'error',
      'no-alert': 'warn',
      'no-var': 'error',
      'prefer-const': 'error',
      'eqeqeq': ['error', 'always'],
      'curly': ['error', 'all'],
    },
  },
  {
    ignores: [
      'node_modules/',
      'dist/',
      '../src/family_assistant/static/dist/',
      'vite.config.js',
      '*.min.js',
    ],
  }
);
