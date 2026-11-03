import { sharedReplyId } from '../ShareMessageDialog';
import type { BackendConversationMessage } from '../types';

function row(
  internalId: number,
  role: string,
  fields: Partial<BackendConversationMessage> = {}
): BackendConversationMessage {
  return {
    internal_id: String(internalId),
    turn_id: 'turn',
    role,
    content: '',
    timestamp: '2026-11-03T00:00:00Z',
    ...fields,
  } as BackendConversationMessage;
}

describe('sharedReplyId', () => {
  it("names the turn's last assistant row with text", () => {
    const rows = [
      row(1, 'user', { content: 'Question' }),
      row(2, 'assistant', { content: 'Checking' }),
      row(3, 'tool', { content: 'output' }),
      row(4, 'assistant', { content: 'Answer' }),
    ];

    expect(sharedReplyId(rows, 'turn')).toBe(4);
  });

  it('names a reply that is only an attachment', () => {
    const rows = [
      row(1, 'user', { content: 'Draw it' }),
      row(2, 'assistant', { attachments: [{ attachment_id: 'picture' }] }),
    ];

    expect(sharedReplyId(rows, 'turn')).toBe(2);
  });

  it('names nothing rather than the question when no reply is stored yet', () => {
    const rows = [row(1, 'user', { content: 'Question' }), row(2, 'assistant')];

    expect(sharedReplyId(rows, 'turn')).toBeNull();
  });
});
