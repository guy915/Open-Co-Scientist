import {PageMetadata} from '@/shared/hooks/use_page_metadata';

export {NotFoundPage} from '@/shared/ui';

export function NoIndex({title}: {title: string}) {
  return (
    <PageMetadata
      title={`${title} - Open Co-Scientist`}
      description="Open Co-Scientist research workspace."
      indexable={false}
    />
  );
}
