export const EMBEDDED_PAGE_BASE = '/api/app/pages';

export function isEmbeddedPage(): boolean {
  return (
    window.location.pathname === EMBEDDED_PAGE_BASE ||
    window.location.pathname.startsWith(`${EMBEDDED_PAGE_BASE}/`)
  );
}

export function pageHref(href: string): string {
  if (
    isEmbeddedPage() &&
    href.startsWith('/') &&
    !href.startsWith('//') &&
    !href.startsWith('/api/')
  ) {
    return `${EMBEDDED_PAGE_BASE}${href}`;
  }
  return href;
}
