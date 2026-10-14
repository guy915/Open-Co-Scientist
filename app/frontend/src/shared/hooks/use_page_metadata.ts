import {useEffect} from 'react';

export interface PageMetadataProps {
  title: string;
  description: string;
  indexable: boolean;
  canonical?: string;
}

function setMeta(
  attribute: 'name' | 'property',
  key: string,
  content: string | undefined,
) {
  const selector = `meta[${attribute}="${key}"]`;
  let element = document.head.querySelector<HTMLMetaElement>(selector);
  if (!content) {
    element?.remove();
    return;
  }
  if (!element) {
    element = document.createElement('meta');
    element.setAttribute(attribute, key);
    document.head.appendChild(element);
  }
  element.content = content;
}

function setCanonical(canonical: string | undefined) {
  let link = document.head.querySelector<HTMLLinkElement>(
    'link[rel="canonical"]',
  );
  if (!canonical) {
    link?.remove();
    return;
  }
  if (!link) {
    link = document.createElement('link');
    link.rel = 'canonical';
    document.head.appendChild(link);
  }
  link.href = canonical;
}

export function usePageMetadata({
  title,
  description,
  indexable,
  canonical,
}: PageMetadataProps) {
  useEffect(() => {
    const robots = indexable ? 'index, follow' : 'noindex, nofollow';
    const publicCanonical = indexable ? canonical : undefined;
    document.title = title;
    setMeta('name', 'description', description);
    setMeta('name', 'robots', robots);
    setMeta('name', 'googlebot', robots);
    setMeta('property', 'og:title', title);
    setMeta('property', 'og:description', description);
    setMeta('property', 'og:url', publicCanonical);
    setMeta('name', 'twitter:title', title);
    setMeta('name', 'twitter:description', description);
    setCanonical(publicCanonical);
  }, [title, description, indexable, canonical]);
}

export function PageMetadata(props: PageMetadataProps) {
  usePageMetadata(props);
  return null;
}
