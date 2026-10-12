import {useEffect} from 'react';
import {Link} from 'react-router-dom';

export function NotFoundPage() {
  return (
    <>
      <NoIndex title="Page Not Found" />
      <section className="mx-auto min-h-[70vh] w-[min(100%_-_3rem,76rem)] py-[clamp(5rem,12vw,9rem)]">
        <p className="mb-4 text-xs font-semibold tracking-[0.07em] text-th-primary uppercase">
          404
        </p>
        <h1 className="m-0 max-w-[12ch] text-[clamp(2.5rem,5.2vw,4rem)] leading-none tracking-normal">
          Page not found
        </h1>
        <p className="mt-6 max-w-[36rem] text-[1.1rem] text-th-muted-fg">
          The page you requested does not exist.
        </p>
        <div className="mt-8 flex flex-wrap gap-3 max-sm:grid max-sm:grid-cols-1">
          <Link
            className="inline-flex min-h-12 items-center justify-center rounded-full border border-transparent bg-th-primary px-5 py-3 text-sm font-semibold leading-none text-th-primary-fg no-underline hover:opacity-90 max-sm:w-full focus-visible:outline-2 focus-visible:outline-offset-[3px] focus-visible:outline-th-primary"
            to="/"
          >
            Return home
          </Link>
        </div>
      </section>
    </>
  );
}

function upsertMeta(name: string, content: string) {
  const selector = `meta[name="${name}"]`;
  let element = document.head.querySelector<HTMLMetaElement>(selector);
  if (!element) {
    element = document.createElement('meta');
    element.setAttribute('name', name);
    document.head.appendChild(element);
  }
  element.content = content;
}

// Every route stays non-indexable; retain head tags on unmount so the next
// route overwrites them without an indexable gap.
export function NoIndex({title}: {title: string}) {
  useEffect(() => {
    document.title = `${title} - Open Co-Scientist`;
    upsertMeta('description', 'Open Co-Scientist research workspace.');
    upsertMeta('robots', 'noindex, nofollow');
    upsertMeta('googlebot', 'noindex, nofollow');
  }, [title]);

  return null;
}
