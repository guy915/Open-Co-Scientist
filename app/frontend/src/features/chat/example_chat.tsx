import {useEffect, useState} from 'react';
import {useNavigate, useParams} from 'react-router-dom';
import {openExampleChat} from '@/shared/api/runs';
import {Button, StatusText} from '@/shared/ui';
import {
  useChatHistoryContext,
  useRunHistoryContext,
} from '@/shared/hooks/history_context';
import {chatPath} from '@/shared/lib/routes';

export function ExampleChat() {
  const {id} = useParams<{id: string}>();
  const navigate = useNavigate();
  const {reload: reloadChats} = useChatHistoryContext();
  const {reload: reloadRuns} = useRunHistoryContext();
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    void (async () => {
      try {
        if (!id) throw new Error('Example not found');
        const interview = await openExampleChat(id);
        await Promise.all([reloadChats(), reloadRuns()]);
        if (!cancelled) await navigate(chatPath(interview.id), {replace: true});
      } catch {
        if (!cancelled)
          setError('The example could not be opened. Please try again.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, attempt, navigate, reloadChats, reloadRuns]);
  return (
    <div className="p-6 text-cosci-text">
      {error ? (
        <>
          <StatusText tone="danger">{error}</StatusText>
          <Button
            variant="outlined"
            onClick={() => setAttempt(value => value + 1)}
          >
            Try again
          </Button>
        </>
      ) : (
        <StatusText>Opening example chat…</StatusText>
      )}
    </div>
  );
}
