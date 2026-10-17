import {Link} from 'react-router-dom';
import {usePageMetadata} from '@/shared/hooks/use_page_metadata';
import {PrivacyContent} from './privacy_content';

export function PrivacyPage() {
  usePageMetadata({
    title: 'Privacy notice - Open Co-Scientist',
    description: 'Privacy notice for Open Co-Scientist.',
    indexable: true,
    canonical: 'https://open-coscientist.com/privacy',
  });
  return (
    <article className="mx-auto w-full max-w-3xl box-border px-6 py-8 leading-relaxed text-th-fg wrap-anywhere [&_h2]:mt-8 [&_h2]:text-xl [&_h2]:font-medium [&_p]:my-4 [&_a]:text-th-primary">
      <nav aria-label="Privacy notice">
        <Link to="/">Research workspace</Link>
      </nav>
      <h1 className="mt-6 text-3xl font-medium">Privacy notice</h1>
      <p className="text-th-muted-fg">Updated · 17 October 2026</p>
      <PrivacyContent />
    </article>
  );
}
