import {describe, it, expect} from 'vitest';
import {render, waitFor} from '@testing-library/react';
import {NoIndex} from './not_found_page';

describe('no index', () => {
  describe('NoIndex', () => {
    // NoIndex renders head metadata through an effect rather than DOM content.

    it('marks the page as noindex via the robots meta tag', async () => {
      render(<NoIndex title="Private" />);
      await waitFor(() => {
        const robots = document.head.querySelector('meta[name="robots"]');
        expect(robots).not.toBeNull();
        expect(robots).toHaveAttribute('content', 'noindex, nofollow');
      });
    });

    it('removes the public canonical and social URL from private routes', async () => {
      const canonical = document.createElement('link');
      canonical.rel = 'canonical';
      canonical.href = 'https://open-coscientist.com';
      document.head.append(canonical);
      const openGraphUrl = document.createElement('meta');
      openGraphUrl.setAttribute('property', 'og:url');
      openGraphUrl.content = 'https://open-coscientist.com';
      document.head.append(openGraphUrl);

      render(<NoIndex title="Private" />);

      await waitFor(() => {
        expect(document.head.querySelector('link[rel="canonical"]')).toBeNull();
        expect(
          document.head.querySelector('meta[property="og:url"]'),
        ).toBeNull();
      });
    });
  });
});
