import {useEffect} from 'react';
import {Link} from 'react-router-dom';
import {PrivacyContent} from './privacy_content';

export function PrivacyPage() {
  useEffect(() => {
    document.title = 'Privacy notice - Open Co-Scientist';
    const updates = [
      ['meta[name="robots"]', 'content', 'index, follow'],
      ['meta[name="googlebot"]', 'content', 'index, follow'],
      [
        'meta[name="description"]',
        'content',
        'Privacy notice for Open Co-Scientist.',
      ],
      ['link[rel="canonical"]', 'href', 'https://open-coscientist.com/privacy'],
    ];
    const previous = updates.map(([selector, attribute, value]) => {
      const element = document.head.querySelector(selector);
      const oldValue = element?.getAttribute(attribute);
      element?.setAttribute(attribute, value);
      return {element, attribute, oldValue};
    });
    return () => {
      previous.forEach(({element, attribute, oldValue}) => {
        if (oldValue !== null && oldValue !== undefined)
          element?.setAttribute(attribute, oldValue);
      });
    };
  }, []);
  return (
    <article className="mx-auto w-full max-w-3xl box-border px-6 py-8 leading-relaxed text-th-fg wrap-anywhere [&_h2]:mt-8 [&_h2]:text-xl [&_h2]:font-medium [&_p]:my-4 [&_a]:text-th-primary">
      <nav aria-label="Privacy notice">
        <Link to="/">Research workspace</Link>
      </nav>
      <h1 className="mt-6 text-3xl font-medium">Privacy notice</h1>
      <p className="text-th-muted-fg">Updated · 12 October 2026</p>
      <PrivacyContent />
    </article>
  );
}
