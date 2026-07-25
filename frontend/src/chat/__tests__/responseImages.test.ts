import { collectResponseImages } from '../responseImages';

describe('collectResponseImages', () => {
  it('returns nothing for content without tool calls', () => {
    expect(collectResponseImages([{ type: 'text', text: 'hello' }])).toEqual([]);
    expect(collectResponseImages('hello')).toEqual([]);
    expect(collectResponseImages(undefined)).toEqual([]);
  });

  it('collects images from a tool-call part', () => {
    const images = collectResponseImages([
      { type: 'text', text: 'Here is your chart' },
      {
        type: 'tool-call',
        toolName: 'attach_to_response',
        attachments: [
          {
            attachment_id: 'att-1',
            description: 'Sales chart',
            content_url: '/api/attachments/att-1',
            mime_type: 'image/png',
          },
        ],
      },
    ]);

    expect(images).toEqual([
      {
        key: 'att-1',
        url: '/api/attachments/att-1',
        name: 'Sales chart',
        mimeType: 'image/png',
      },
    ]);
  });

  it('reads attachments from the tool artifact when the part has none', () => {
    const images = collectResponseImages([
      {
        type: 'tool-call',
        toolName: 'attach_to_response',
        artifact: {
          attachments: [
            { attachment_id: 'att-2', mime_type: 'image/jpeg', url: '/api/attachments/att-2' },
          ],
        },
      },
    ]);

    expect(images).toHaveLength(1);
    expect(images[0]?.url).toBe('/api/attachments/att-2');
    // Nothing describes the attachment, so it gets the generic label.
    expect(images[0]?.name).toBe('Attachment');
  });

  it('derives the URL from the attachment id when no URL is supplied', () => {
    const images = collectResponseImages([
      {
        type: 'tool-call',
        toolName: 'generate_image',
        attachments: [{ attachment_id: 'a b/c', content_type: 'image/webp' }],
      },
    ]);

    expect(images[0]?.url).toBe('/api/attachments/a%20b%2Fc');
    expect(images[0]?.mimeType).toBe('image/webp');
  });

  it('skips non-image attachments and attachments it cannot address', () => {
    const images = collectResponseImages([
      {
        type: 'tool-call',
        toolName: 'attach_to_response',
        attachments: [
          { attachment_id: 'doc-1', mime_type: 'application/pdf' },
          { attachment_id: 'unknown-1' },
          { mime_type: 'image/png', description: 'No id and no url' },
          'not-an-object',
        ],
      },
    ]);

    expect(images).toEqual([]);
  });

  it('deduplicates the same attachment reported by several parts', () => {
    const attachment = {
      attachment_id: 'att-3',
      mime_type: 'image/png',
      content_url: '/api/attachments/att-3',
    };
    const images = collectResponseImages([
      { type: 'tool-call', toolName: 'generate_image', attachments: [attachment] },
      { type: 'tool-call', toolName: 'attach_to_response', attachments: [attachment] },
    ]);

    expect(images).toHaveLength(1);
  });

  it('keeps every distinct image in order', () => {
    const images = collectResponseImages([
      {
        type: 'tool-call',
        toolName: 'attach_to_response',
        attachments: [
          { attachment_id: 'att-4', mime_type: 'image/png', content_url: '/api/attachments/att-4' },
          { attachment_id: 'att-5', mime_type: 'image/gif', content_url: '/api/attachments/att-5' },
        ],
      },
    ]);

    expect(images.map((image) => image.key)).toEqual(['att-4', 'att-5']);
  });
});
