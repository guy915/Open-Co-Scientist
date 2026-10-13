import {useEffect, useRef, useState} from 'react';

// Keep one mounted, initially empty polite region. Partial prose and private
// reasoning stay outside it; only a new reply from a completed turn enters it.
export function SettledReply({
  busy,
  reply,
  revision,
}: {
  busy: boolean;
  reply: string;
  revision: string;
}) {
  const [announcement, setAnnouncement] = useState('');
  const previous = useRef({busy: false, revision});
  useEffect(() => {
    const before = previous.current;
    if (busy && !before.busy) setAnnouncement('');
    if (!busy && before.busy && revision !== before.revision && reply.trim()) {
      setAnnouncement(reply);
    }
    // Freeze the baseline throughout streaming, including settlement renders
    // that append the durable message before clearing the busy flag.
    previous.current = {
      busy,
      revision: busy && before.busy ? before.revision : revision,
    };
  }, [busy, reply, revision]);
  return (
    <span
      className="sr-only"
      role="status"
      aria-label="Research reply"
      aria-live="polite"
      aria-atomic="true"
    >
      {announcement}
    </span>
  );
}
