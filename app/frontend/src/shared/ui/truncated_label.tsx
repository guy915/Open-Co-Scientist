import {type RefObject, useLayoutEffect, useRef} from 'react';
import {ellipsize} from '@/shared/lib/text';

// Fonts, unsettled first-frame layout and suspended background observers can
// change metrics; re-fit on their return before relying on a measurement.
function scheduleFits(fit: () => void, el: HTMLSpanElement): () => void {
  fit();
  const raf = requestAnimationFrame(fit);
  let cancelled = false;
  void document.fonts?.ready.then(() => {
    if (!cancelled) fit();
  });
  // Defer writes out of ResizeObserver delivery to avoid observer loops;
  // an idempotent next-frame fit settles layout.
  let queued = 0;
  const observer = new ResizeObserver(() => {
    cancelAnimationFrame(queued);
    queued = requestAnimationFrame(fit);
  });
  observer.observe(el);
  const onVisible = () => {
    if (!document.hidden) fit();
  };
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    cancelled = true;
    cancelAnimationFrame(raf);
    cancelAnimationFrame(queued);
    observer.disconnect();
    document.removeEventListener('visibilitychange', onVisible);
  };
}

// Batch fits in a microtask before paint so labels mounted together share
// layout flushes without showing an unfitted frame.
function useTruncatedFit(
  ref: RefObject<HTMLSpanElement | null>,
  text: string,
  lines: number,
) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    function fit() {
      const node = ref.current;
      if (!node) return;
      requestFit(node, text, lines);
    }

    return scheduleFits(fit, el);
  }, [ref, text, lines]);
}

// The host CSS constrains the measured axis; imperative text ownership avoids
// React reconciliation, and oversized first words must clip.
export function TruncatedLabel({
  text,
  className,
  lines = 1,
}: {
  text: string;
  className?: string;
  lines?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  useTruncatedFit(ref, text, lines);

  return <span ref={ref} className={className} />;
}

interface FitRequest {
  node: HTMLSpanElement;
  text: string;
  lines: number;
}

interface FitJob {
  node: HTMLSpanElement;
  lines: number;
  words: string[];
  lo: number;
  hi: number;
  mid: number;
  best: number;
}

// One-pixel slack absorbs fractional layout rounding rather than reporting
// permanent borderline overflow.
function overflows(node: HTMLSpanElement, lines: number): boolean {
  return lines > 1
    ? node.scrollHeight > node.clientHeight + 1
    : node.scrollWidth > node.clientWidth + 1;
}

function candidate(job: FitJob, count: number): string {
  return ellipsize(job.words.slice(0, count).join(' '));
}

// Separate full-text writes from overflow reads to pay one layout flush for the
// entire batch.
function startJobs(requests: readonly FitRequest[]): FitJob[] {
  for (const request of requests) request.node.textContent = request.text;
  const jobs: FitJob[] = [];
  for (const request of requests) {
    if (!overflows(request.node, request.lines)) continue;
    const words = request.text.split(/\s+/).filter(Boolean);
    jobs.push({
      node: request.node,
      lines: request.lines,
      words,
      lo: 1,
      hi: words.length - 1,
      mid: 0,
      best: 0,
    });
  }
  return jobs;
}

// All candidate writes precede reads; preserve this order to avoid a layout
// flush per label.
function probeRound(jobs: readonly FitJob[]): FitJob[] {
  for (const job of jobs) {
    job.mid = (job.lo + job.hi) >> 1;
    job.node.textContent = candidate(job, job.mid);
  }
  for (const job of jobs) {
    if (overflows(job.node, job.lines)) {
      job.hi = job.mid - 1;
    } else {
      job.best = job.mid;
      job.lo = job.mid + 1;
    }
  }
  return jobs.filter(job => job.lo <= job.hi);
}

function commitJob(job: FitJob): void {
  // Even the first word can exceed the box and must be clipped.
  job.node.textContent = candidate(job, Math.max(job.best, 1));
}

function fitAll(requests: readonly FitRequest[]): void {
  const jobs = startJobs(requests);
  let searching: readonly FitJob[] = jobs;
  while (searching.length) searching = probeRound(searching);
  for (const job of jobs) commitJob(job);
}

const pending = new Map<HTMLSpanElement, FitRequest>();
let flushQueued = false;

function flushPendingFits(): void {
  flushQueued = false;
  const requests = [...pending.values()].filter(
    request => request.node.isConnected,
  );
  pending.clear();
  if (requests.length) fitAll(requests);
}

// Write current full text immediately while batching only measurement, so
// waiting nodes never show another label or stale text.
function requestFit(node: HTMLSpanElement, text: string, lines: number): void {
  node.textContent = text;
  pending.set(node, {node, text, lines});
  if (flushQueued) return;
  flushQueued = true;
  queueMicrotask(flushPendingFits);
}
