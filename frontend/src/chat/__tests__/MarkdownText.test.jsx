import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MarkdownText } from '../MarkdownText';

describe('MarkdownText', () => {
  it('renders inline code correctly', () => {
    const text = 'The `Foo` service';
    const { container } = render(<MarkdownText text={text} />);

    // Check if it contains the code element
    const codeElement = container.querySelector('code');
    expect(codeElement).toBeTruthy();
    expect(codeElement?.textContent).toBe('Foo');

    // Check if it has the inline-code class
    expect(codeElement?.classList.contains('inline-code')).toBe(true);

    // Ensure it's not wrapped in a pre (which would make it a block)
    expect(container.querySelector('pre')).toBeNull();
  });

  it('renders code blocks correctly', () => {
    const text = '```\nconst x = 1;\n```';
    const { container } = render(<MarkdownText text={text} />);

    // Check if it contains the pre and code element
    const preElement = container.querySelector('pre');
    expect(preElement).toBeTruthy();
    expect(preElement?.classList.contains('code-block')).toBe(true);

    const codeElement = preElement?.querySelector('code');
    expect(codeElement).toBeTruthy();
    expect(codeElement?.textContent?.trim()).toBe('const x = 1;');
  });
});
