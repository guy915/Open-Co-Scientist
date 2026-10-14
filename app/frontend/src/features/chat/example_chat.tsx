import {useEffect, useState} from 'react';
import {Link, useNavigate, useParams} from 'react-router-dom';
import {
  listPublicExamples,
  type PublicExample,
} from '@/shared/api/public_examples';
import {openExampleChat} from '@/shared/api/runs';
import {
  useChatHistoryContext,
  useRunHistoryContext,
} from '@/shared/hooks/history_context';
import {PageMetadata} from '@/shared/hooks/use_page_metadata';
import {Button, NotFoundPage, StatusText} from '@/shared/ui';
import {chatPath} from '@/shared/lib/routes';

export function ExampleChat() {
  const {id} = useParams<{id: string}>();
  const navigate = useNavigate();
  const {reload: reloadChats} = useChatHistoryContext();
  const {reload: reloadRuns} = useRunHistoryContext();
  const [examples, setExamples] = useState<PublicExample[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [opening, setOpening] = useState(false);
  const [openFailed, setOpenFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void listPublicExamples()
      .then(value => {
        if (!cancelled) setExamples(value);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const example = examples?.find(item => item.id === id);

  async function openExample() {
    if (!id || opening) return;
    setOpening(true);
    setOpenFailed(false);
    try {
      const interview = await openExampleChat(id);
      await Promise.all([reloadChats(), reloadRuns()]);
      await navigate(chatPath(interview.id), {replace: true});
    } catch {
      setOpening(false);
      setOpenFailed(true);
    }
  }

  if (examples === null) {
    return (
      <div
        className="p-6 text-cosci-text"
        role="status"
        aria-busy={!loadFailed}
      >
        {loadFailed ? (
          <>
            <PageMetadata
              title="Example unavailable - Open Co-Scientist"
              description="Open Co-Scientist example chat."
              indexable={false}
            />
            <StatusText tone="danger">
              The public examples could not be loaded. Please refresh the page.
            </StatusText>
          </>
        ) : (
          'Loading example…'
        )}
      </div>
    );
  }

  if (!example) {
    return <NotFoundPage />;
  }

  return (
    <>
      <PageMetadata
        title={example.title}
        description={example.description}
        indexable
        canonical={`https://open-coscientist.com/examples/${encodeURIComponent(example.id)}`}
      />
      <article className="mx-auto w-full max-w-3xl box-border px-6 py-8 leading-relaxed text-th-fg wrap-anywhere">
        <nav aria-label="Example chat">
          <Link to="/">Research workspace</Link>
        </nav>
        <h1 className="mt-6 text-3xl font-medium">{example.heading}</h1>
        <h2 className="mt-6 text-xl font-medium">Research goal</h2>
        <p className="my-4 text-th-muted-fg">{example.goal}</p>
        <h2 className="mt-6 text-xl font-medium">Example overview</h2>
        <p className="my-4 text-th-muted-fg">{example.summary}</p>
        <h2 className="mt-6 text-xl font-medium">Leading hypotheses</h2>
        <ol className="my-4 grid gap-4 pl-6">
          {example.ideas.map((idea, index) => (
            <li key={`${idea.title}-${index}`}>
              <h3 className="m-0 text-base font-medium">{idea.title}</h3>
              <p className="my-2 text-th-muted-fg">{idea.statement}</p>
            </li>
          ))}
        </ol>
        {openFailed && (
          <StatusText tone="danger">
            The example could not be opened. Please try again.
          </StatusText>
        )}
        <Button onClick={() => void openExample()} disabled={opening}>
          {opening ? 'Opening example…' : 'Open example'}
        </Button>
      </article>
    </>
  );
}
