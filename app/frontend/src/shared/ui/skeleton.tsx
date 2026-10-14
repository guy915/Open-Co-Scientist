import type {ReactNode} from 'react';
import {joinClasses} from './cx';

type SkeletonShape = 'line' | 'block' | 'circle';

const SHAPE_CLASSES: Record<SkeletonShape, string> = {
  line: 'h-3.5 rounded-full',
  block: 'rounded-2xl',
  circle: 'aspect-square rounded-full',
};

// One placeholder shape. Its region announces the loading state, so the shape
// itself stays out of the accessibility tree.
function Skeleton({
  shape = 'line',
  layoutClassName,
}: {
  shape?: SkeletonShape;
  layoutClassName?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={joinClasses(
        'ui-skeleton block shrink-0',
        SHAPE_CLASSES[shape],
        layoutClassName,
      )}
    />
  );
}

// Wraps the placeholder layout of an area that is still loading and names it
// once for assistive technology.
function SkeletonRegion({
  label,
  layoutClassName,
  children,
}: {
  label: string;
  layoutClassName?: string;
  children: ReactNode;
}) {
  return (
    <div aria-busy="true" className={layoutClassName}>
      <p role="status" className="sr-only">
        {label}
      </p>
      {children}
    </div>
  );
}

const TEXT_LINE_WIDTHS = ['w-[92%]', 'w-[97%]', 'w-full'];

function SkeletonText({lines}: {lines: number}) {
  return (
    <div className="grid gap-3.5">
      {Array.from({length: lines}, (_, index) => (
        <Skeleton
          key={index}
          layoutClassName={
            index === lines - 1
              ? 'w-[64%]'
              : TEXT_LINE_WIDTHS[index % TEXT_LINE_WIDTHS.length]
          }
        />
      ))}
    </div>
  );
}

// A loading document in the report column: a heading over paragraphs, the
// shape a lazy page or the report body takes before its content arrives.
export function DocumentSkeleton({label}: {label: string}) {
  return (
    <SkeletonRegion
      label={label}
      layoutClassName="ui-motion-enter mx-auto mt-9 grid w-[min(100%_-_3rem,58rem)] content-start gap-7 phone:mt-5 phone:w-[min(100%_-_1.2rem,100%)]"
    >
      <Skeleton layoutClassName="h-9 w-[min(18rem,70%)]" />
      <SkeletonText lines={4} />
      <Skeleton layoutClassName="mt-2 h-6 w-[min(12rem,50%)]" />
      <SkeletonText lines={3} />
    </SkeletonRegion>
  );
}
