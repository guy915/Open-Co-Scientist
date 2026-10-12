import type {ComponentPropsWithoutRef, ReactNode} from 'react';

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

// Model-written URLs reach these links, so anything but a web or mail address
// (javascript:, data:, relative paths) renders as plain content instead.
export function safeExternalHref(
  href: string | null | undefined,
): string | null {
  if (!href) return null;
  try {
    return SAFE_PROTOCOLS.has(new URL(href).protocol) ? href : null;
  } catch {
    return null;
  }
}

// Opens in a new tab without exposing this window or the referrer.
export function ExternalLink({
  href,
  fallback = null,
  children,
  ...rest
}: Omit<ComponentPropsWithoutRef<'a'>, 'href' | 'target' | 'rel'> & {
  href: string | null | undefined;
  fallback?: ReactNode;
}) {
  const safe = safeExternalHref(href);
  if (!safe) return <>{fallback}</>;
  return (
    <a {...rest} href={safe} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}
