import React from 'react';
import { render } from '@testing-library/react';
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

  it('wraps output in a markdown-text container for scoped styling', () => {
    const { container } = render(<MarkdownText text="hello" />);

    expect(container.querySelector('.markdown-text')).toBeTruthy();
  });

  it('renders separate paragraphs for blank-line-separated text', () => {
    const text = 'First paragraph.\n\nSecond paragraph.';
    const { container } = render(<MarkdownText text={text} />);

    const paragraphs = container.querySelectorAll('.markdown-text > p');
    expect(paragraphs.length).toBe(2);
    expect(paragraphs[0]?.textContent).toBe('First paragraph.');
    expect(paragraphs[1]?.textContent).toBe('Second paragraph.');
  });

  it('renders links as safe external anchors', () => {
    const text = 'See the [family calendar](https://example.com/cal).';
    const { container } = render(<MarkdownText text={text} />);

    const link = container.querySelector('a');
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toBe('https://example.com/cal');
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link?.textContent).toBe('family calendar');
  });

  it('renders unordered lists with list items', () => {
    const text = '- Apples\n- Oranges\n- Pears';
    const { container } = render(<MarkdownText text={text} />);

    const list = container.querySelector('ul');
    expect(list).toBeTruthy();
    expect(list?.querySelectorAll('li').length).toBe(3);
  });

  it('renders ordered lists', () => {
    const text = '1. First\n2. Second';
    const { container } = render(<MarkdownText text={text} />);

    const list = container.querySelector('ol');
    expect(list).toBeTruthy();
    expect(list?.querySelectorAll('li').length).toBe(2);
  });

  it('renders headings', () => {
    const text = '# Big title\n\nSome body text.';
    const { container } = render(<MarkdownText text={text} />);

    const heading = container.querySelector('h1');
    expect(heading).toBeTruthy();
    expect(heading?.textContent).toBe('Big title');
  });

  it('renders blockquotes', () => {
    const text = '> Remember to arrive early.';
    const { container } = render(<MarkdownText text={text} />);

    const quote = container.querySelector('blockquote');
    expect(quote).toBeTruthy();
    expect(quote?.textContent?.trim()).toBe('Remember to arrive early.');
  });

  it('renders GitHub-flavored markdown tables', () => {
    const text = '| Item | Done |\n| --- | --- |\n| Tent | yes |';
    const { container } = render(<MarkdownText text={text} />);

    const table = container.querySelector('table');
    expect(table).toBeTruthy();
    expect(table?.querySelectorAll('th').length).toBe(2);
    expect(table?.querySelectorAll('tbody td').length).toBe(2);
  });
});
