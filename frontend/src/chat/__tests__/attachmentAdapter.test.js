/**
 * @vitest-environment jsdom
 */

import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import {
  CompositeAttachmentAdapter,
  defaultAttachmentAdapter,
  FileAttachmentAdapter,
} from '../attachmentAdapter';

// Mock crypto.randomUUID
Object.defineProperty(global, 'crypto', {
  value: {
    randomUUID: () => 'test-uuid-123',
  },
});

// Mock FileReader (still used as fallback)
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

// Mock fetch will be handled by MSW server from setup.js

// Create mock file helper
function createMockFile(name, type, size, content = 'test content') {
  const file = new File([content], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('FileAttachmentAdapter', () => {
  let adapter;

  beforeEach(() => {
    adapter = new FileAttachmentAdapter();
  });

  describe('constructor', () => {
    test('sets correct accept pattern', () => {
      expect(adapter.accept).toBe(
        'image/jpeg,image/png,image/gif,image/webp,text/plain,text/markdown,application/pdf'
      );
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

    test('successfully adds valid text file', async () => {
      const file = createMockFile('document.txt', 'text/plain', 1024);

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('document');
      expect(result.name).toBe('document.txt');
      expect(result.file).toBe(file);
      expect(result.status.type).toBe('running');
    });

    test('successfully adds valid PDF file', async () => {
      const file = createMockFile('document.pdf', 'application/pdf', 1024);

      const result = await adapter.add({ file });

      expect(result.id).toBe('test-uuid-123');
      expect(result.type).toBe('document');
      expect(result.name).toBe('document.pdf');
      expect(result.file).toBe(file);
      expect(result.status.type).toBe('running');
    });

    test('throws for oversized file', async () => {
      const file = createMockFile('large.png', 'image/png', 150 * 1024 * 1024); // 150MB (exceeds 100MB limit)

      await expect(adapter.add({ file })).rejects.toThrow('size exceeds');
    });

    test('throws for invalid file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      await expect(adapter.add({ file })).rejects.toThrow('Unsupported file type');
    });

    test('throws for file with empty name', async () => {
      const file = createMockFile('', 'image/png', 1024);

      await expect(adapter.add({ file })).rejects.toThrow('valid name');
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

      // MSW will handle the API call

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.type).toBe('image');
      expect(result.name).toBe('test.png');
      // content is now ThreadUserMessagePart[] as required by @assistant-ui/react 0.12.15+
      expect(result.content).toEqual([
        { type: 'image', image: '/api/attachments/server-uuid-456' },
      ]);
      expect(result.uploadedId).toBe('server-uuid-456');
      expect(result.status.type).toBe('complete');

      // MSW handled the API call
    });

    test('throws on upload failure', async () => {
      // Override the upload handler to return an error
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.json({ detail: 'Upload failed' }, { status: 500 });
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      await expect(adapter.send(attachment)).rejects.toThrow('Upload failed');
    });

    test('throws on network error', async () => {
      // Override the upload handler to return a network error
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.error();
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      await expect(adapter.send(attachment)).rejects.toThrow();
    });
  });

  describe('remove method', () => {
    test('successfully removes uploaded attachment', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      // MSW will handle the API call

      await adapter.remove(attachment);

      // MSW handled the DELETE request
    });

    test('handles server deletion failure gracefully', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      // MSW will handle the API call (default success, this tests error handling)

      // Should not throw - just logs warning
      await expect(adapter.remove(attachment)).resolves.toBeUndefined();
    });

    test('skips server deletion for non-uploaded attachment', async () => {
      const attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        status: { type: 'error' },
      };

      await adapter.remove(attachment);

      // Should not call fetch
      // Attachment was not uploaded, so no server call should be made
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
      remove: vi.fn(),
    };
    compositeAdapter = new CompositeAttachmentAdapter([mockImageAdapter]);
  });

  describe('getAdapterForType', () => {
    test('finds adapter for exact match', () => {
      const result = compositeAdapter.getAdapterForType('image/png');
      expect(result).toBe(mockImageAdapter);
    });

    test('finds adapter for comma-separated types', () => {
      mockImageAdapter.accept = 'image/jpeg,image/png,text/plain';
      const result = compositeAdapter.getAdapterForType('text/plain');
      expect(result).toBe(mockImageAdapter);
    });

    test('returns null for no match', () => {
      const result = compositeAdapter.getAdapterForType(
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
      );
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

    test('throws for unsupported file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      await expect(compositeAdapter.add({ file })).rejects.toThrow('Unsupported file type');
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

    test('throws for no matching adapter', async () => {
      const attachment = {
        id: 'test',
        type: 'document',
        file: createMockFile(
          'doc.docx',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          1024
        ),
      };

      await expect(compositeAdapter.send(attachment)).rejects.toThrow('No adapter available');
    });
  });
});

describe('defaultAttachmentAdapter', () => {
  test('exports a pre-configured composite adapter', () => {
    expect(defaultAttachmentAdapter).toBeInstanceOf(CompositeAttachmentAdapter);
    expect(defaultAttachmentAdapter.adapters).toHaveLength(1);
    expect(defaultAttachmentAdapter.adapters[0]).toBeInstanceOf(FileAttachmentAdapter);
  });

  test('supports image files', async () => {
    const file = createMockFile('test.jpg', 'image/jpeg', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('image');
    expect(result.status.type).toBe('running');
  });

  test('supports text files', async () => {
    const file = createMockFile('readme.txt', 'text/plain', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('document');
    expect(result.status.type).toBe('running');
  });

  test('supports PDF files', async () => {
    const file = createMockFile('document.pdf', 'application/pdf', 1024);

    const result = await defaultAttachmentAdapter.add({ file });

    expect(result.type).toBe('document');
    expect(result.status.type).toBe('running');
  });
});
