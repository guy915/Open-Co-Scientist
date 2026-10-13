import {useEffect, useState} from 'react';
import {copyText} from '@/shared/lib/clipboard';
import {IconButton, type IconButtonProps} from './icon_button';

const COPIED_MS = 2_000;

// Every copy control: the icon turns into a check and the name into "Copied"
// for two seconds, and only when the copy actually succeeded.
export function CopyButton({
  text,
  label,
  ...rest
}: Omit<IconButtonProps, 'icon' | 'label' | 'onClick'> & {
  text: string;
  label: string;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), COPIED_MS);
    return () => window.clearTimeout(id);
  }, [copied]);
  return (
    <IconButton
      {...rest}
      icon={copied ? 'check' : 'content_copy'}
      label={copied ? 'Copied' : label}
      onClick={() => void copyText(text).then(setCopied)}
    />
  );
}
