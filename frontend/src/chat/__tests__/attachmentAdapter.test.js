/**
 * @vitest-environment jsdom
 */

import { vi } from 'vitest';
import {
  SimpleImageAttachmentAdapter,
  CompositeAttachmentAdapter,
  defaultAttachmentAdapter,
} from '../attachmentAdapter';

// Mock crypto.randomUUID
Object.defineProperty(global, 'crypto', {
  value: {
    randomUUID: () => 'test-uuid-123',
  },
});

// Mock FileReader
global.FileReader = class {
  constructor() {
    this.result = null;
    this.onload = null;
    this.onerror = null;
  }

  readAsDataURL(file) {
    // Simulate async behavior
    setTimeout(() => {
      if (this.onload) {
        this.result = `data:${file.type};base64,dGVzdA==`; // "test" in base64
        this.onload();
      }
    }, 0);
  }
};

// Create mock file helper
function createMockFile(name, type, size, content = 'test content') {
  const file = new File([content], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('SimpleImageAttachmentAdapter', () => {
  let adapter;

  beforeEach(() => {
    adapter = new SimpleImageAttachmentAdapter();
  });

  describe('constructor', () => {
    test('sets correct accept pattern', () => {
      expect(adapter.accept).toBe('image/*');
    });
  });

  describe('add method', () => {
    test('successfully adds valid image file', async () => {
      const file = createMockFile('test.png', 'image/png', 1024 * 1024); // 1MB

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('image');
      expect(result.name).toBe('test.png');
      expect(result.file).toBe(file);
      expect(result.status.type).toBe('running');
    });

    test('returns error for oversized file', async () => {
      const file = createMockFile('large.png', 'image/png', 15 * 1024 * 1024); // 15MB

      const result = await adapter.add({ file });

      expect(result.type).toBe('image');
      expect(result.name).toBe('large.png');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('size exceeds');
    });

    test('returns error for invalid file type', async () => {
      const file = createMockFile('document.txt', 'text/plain', 1024);

      const result = await adapter.add({ file });

      expect(result.type).toBe('image');
      expect(result.name).toBe('document.txt');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Unsupported file type');
    });

    test('returns error for file with empty name', async () => {
      const file = createMockFile('', 'image/png', 1024);

      const result = await adapter.add({ file });

      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('valid name');
    });
  });

  describe('send method', () => {
    test('successfully processes attachment', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.type).toBe('image');
      expect(result.name).toBe('test.png');
      expect(result.content).toContain('data:image/png;base64,');
      expect(result.status.type).toBe('complete');
    });

    test('handles FileReader error gracefully', async () => {
      // Mock FileReader to throw error
      const originalFileReader = global.FileReader;
      global.FileReader = class {
        readAsDataURL() {
          setTimeout(() => {
            if (this.onerror) {
              this.onerror(new Error('Read failed'));
            }
          }, 0);
        }
      };

      const file = createMockFile('test.png', 'image/png', 1024);
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Failed to process image');

      // Restore original FileReader
      global.FileReader = originalFileReader;
    });
  });
});

describe('CompositeAttachmentAdapter', () => {
  let mockImageAdapter;
  let compositeAdapter;

  beforeEach(() => {
    mockImageAdapter = {
      accept: 'image/*',
      add: vi.fn(),
      send: vi.fn(),
    };
    compositeAdapter = new CompositeAttachmentAdapter([mockImageAdapter]);
  });

  describe('getAdapterForType', () => {
    test('finds adapter for exact match', () => {
      const result = compositeAdapter.getAdapterForType('image/png');
      expect(result).toBe(mockImageAdapter);
    });

    test('finds adapter for wildcard match', () => {
      const result = compositeAdapter.getAdapterForType('image/jpeg');
      expect(result).toBe(mockImageAdapter);
    });

    test('returns null for no match', () => {
      const result = compositeAdapter.getAdapterForType('text/plain');
      expect(result).toBeUndefined();
    });
  });

  describe('add method', () => {
    test('delegates to matching adapter', async () => {
      const file = createMockFile('test.png', 'image/png', 1024);
      const expectedResult = { id: 'test', type: 'image' };
      mockImageAdapter.add.mockResolvedValue(expectedResult);

      const result = await compositeAdapter.add({ file });

      expect(result).toBe(expectedResult);
      expect(mockImageAdapter.add).toHaveBeenCalledWith({ file });
    });

    test('returns error for unsupported file type', async () => {
      const file = createMockFile('document.txt', 'text/plain', 1024);

      const result = await compositeAdapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('document.txt');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Unsupported file type');
    });
  });

  describe('send method', () => {
    test('delegates to matching adapter', async () => {
      const attachment = {
        id: 'test',
        type: 'image',
        file: createMockFile('test.png', 'image/png', 1024),
      };
      const expectedResult = { id: 'test', status: { type: 'complete' } };
      mockImageAdapter.send.mockResolvedValue(expectedResult);

      const result = await compositeAdapter.send(attachment);

      expect(result).toBe(expectedResult);
      expect(mockImageAdapter.send).toHaveBeenCalledWith(attachment);
    });

    test('returns error for no matching adapter', async () => {
      const attachment = {
        id: 'test',
        type: 'document',
        file: createMockFile('doc.txt', 'text/plain', 1024),
      };

      const result = await compositeAdapter.send(attachment);

      expect(result.id).toBe('test');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('No adapter available');
    });
  });
});

describe('defaultAttachmentAdapter', () => {
  test('exports a pre-configured composite adapter', () => {
    expect(defaultAttachmentAdapter).toBeInstanceOf(CompositeAttachmentAdapter);
    expect(defaultAttachmentAdapter.adapters).toHaveLength(1);
    expect(defaultAttachmentAdapter.adapters[0]).toBeInstanceOf(SimpleImageAttachmentAdapter);
  });

  test('supports image files', async () => {
    const file = createMockFile('test.jpg', 'image/jpeg', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('image');
    expect(result.status.type).toBe('running');
  });
});
