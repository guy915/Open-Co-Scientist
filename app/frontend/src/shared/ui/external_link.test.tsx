import {render, screen} from '@testing-library/react';
import {expect, it} from 'vitest';
import {ExternalLink, safeExternalHref} from './external_link';

it('keeps web and mail addresses only', () => {
  expect(safeExternalHref('https://example.org/a')).toBe(
    'https://example.org/a',
  );
  expect(safeExternalHref('mailto:a@example.org')).toBe('mailto:a@example.org');
  expect(safeExternalHref('javascript:alert(1)')).toBeNull();
  expect(safeExternalHref('data:text/html,x')).toBeNull();
  expect(safeExternalHref('/runs/1')).toBeNull();
  expect(safeExternalHref(undefined)).toBeNull();
});

it('opens safe links in a new tab without the referrer', () => {
  render(<ExternalLink href="https://example.org">Source</ExternalLink>);
  const link = screen.getByRole('link', {name: 'Source'});
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toBe('noopener noreferrer');
});

it('renders the fallback for an unsafe link', () => {
  render(
    <ExternalLink href="javascript:alert(1)" fallback="Source">
      Source
    </ExternalLink>,
  );
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.getByText('Source')).toBeTruthy();
});
