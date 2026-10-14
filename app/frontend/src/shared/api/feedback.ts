import {fetchJson, jsonRequest} from './runs';
import type {FeedbackRequest} from './wire_system';

export type FeedbackCategory = FeedbackRequest['category'];

// Keys keep the menu order; the record makes a new backend category a type
// error here.
const CATEGORIES: Record<FeedbackCategory, true> = {
  Bug: true,
  Security: true,
  'Results quality': true,
  'Feature request': true,
  Other: true,
};
export const FEEDBACK_CATEGORIES = Object.keys(
  CATEGORIES,
) as FeedbackCategory[];

export function submitFeedback(
  submission: FeedbackRequest,
): Promise<{id: string; status: string}> {
  return fetchJson('/api/feedback', jsonRequest(submission, true));
}
