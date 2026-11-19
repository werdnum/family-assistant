import { describe, expect, it } from 'vitest';
import { generateConversationId } from '../uuid';

describe('generateConversationId', () => {
  it('writes the id without dashes so a double-click selects it whole', () => {
    expect(generateConversationId()).toMatch(/^web_conv_[0-9a-f]{32}$/);
  });
});
