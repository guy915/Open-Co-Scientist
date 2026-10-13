import {useState} from 'react';
import {Icon, type IconName} from './icon';
import {joinClasses} from './cx';

// An icon that cross-fades when its name changes (copy → check, stop →
// confirm). The first icon appears without motion.
export function SwapIcon({
  name,
  className,
}: {
  name: IconName;
  className?: string;
}) {
  const [initial] = useState(name);
  const [changed, setChanged] = useState(false);
  if (!changed && name !== initial) setChanged(true);
  return (
    <Icon
      key={name}
      className={joinClasses(className, changed && 'ui-motion-swap')}
      name={name}
    />
  );
}
