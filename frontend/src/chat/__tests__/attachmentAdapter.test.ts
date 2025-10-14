/**
 * @vitest-environment jsdom
 */

import { vi, describe, test, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/setup';
import {
  FileAttachmentAdapter,
  CompositeAttachmentAdapter,
  defaultAttachmentAdapter,
} from '../attachmentAdapter';
import { Attachment } from '@assistant-ui/react';

// Mock crypto.randomUUID
Object.defineProperty(global, 'crypto', {
  value: {
    randomUUID: () => 'test-uuid-123',
  },
});

// Mock FileReader
vi.stubGlobal(
  'FileReader',
  class {
    result: string | ArrayBuffer | null = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;

    readAsDataURL(file: File) {
      setTimeout(() => {
        if (this.onload) {
          this.result = `data:${file.type};base64,dGVzdA==`; // "test" in base64
          this.onload();
        }
      }, 0);
    }
  }
);

// Create mock file helper
function createMockFile(name: string, type: string, size: number, content = 'test content'): File {
  const file = new File([content], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('FileAttachmentAdapter', () => {
  let adapter: FileAttachmentAdapter;

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

    test('returns error for oversized file', async () => {
      const file = createMockFile('large.png', 'image/png', 150 * 1024 * 1024); // 150MB (exceeds 100MB limit)

      const result = await adapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('large.png');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('size exceeds');
    });

    test('returns error for invalid file type', async () => {
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const result = await adapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('document.docx');
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
      const attachment: Attachment = {
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
      expect(result.content).toBe('/api/attachments/server-uuid-456');
      expect(result.uploadedId).toBe('server-uuid-456');
      expect(result.status.type).toBe('complete');
    });

    test('handles upload failure gracefully', async () => {
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.json({ error: 'Upload failed' }, { status: 500 });
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment: Attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Failed to upload file');
    });

    test('handles network error gracefully', async () => {
      server.use(
        http.post('/api/attachments/upload', () => {
          return HttpResponse.error();
        })
      );

      const file = new File(['test content'], 'test.png', { type: 'image/png' });
      const attachment: Attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        file,
        status: { type: 'running' },
      };

      const result = await adapter.send(attachment);

      expect(result.id).toBe('test-id');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Failed to upload file');
    });
  });

  describe('remove method', () => {
    test('successfully removes uploaded attachment', async () => {
      const attachment: Attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      await adapter.remove(attachment);
    });

    test('handles server deletion failure gracefully', async () => {
      const attachment: Attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        uploadedId: 'server-uuid-456',
        status: { type: 'complete' },
      };

      await expect(adapter.remove(attachment)).resolves.toBeUndefined();
    });

    test('skips server deletion for non-uploaded attachment', async () => {
      const attachment: Attachment = {
        id: 'test-id',
        type: 'image',
        name: 'test.png',
        status: { type: 'error' },
      };

      await adapter.remove(attachment);
    });
  });
});

describe('CompositeAttachmentAdapter', () => {
  let mockImageAdapter: any;
  let compositeAdapter: CompositeAttachmentAdapter;

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
      expect(result).toBeNull();
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
      const file = createMockFile(
        'document.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        1024
      );

      const result = await compositeAdapter.add({ file });

      expect(result.type).toBe('file');
      expect(result.name).toBe('document.docx');
      expect(result.status.type).toBe('error');
      expect(result.status.error).toContain('Unsupported file type');
    });
  });

  describe('send method', () => {
    test('delegates to matching adapter', async () => {
      const attachment: Attachment = {
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
      const attachment: Attachment = {
        id: 'test',
        type: 'document',
        file: createMockFile(
          'doc.docx',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          1024
        ),
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
