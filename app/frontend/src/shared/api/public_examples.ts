export interface PublicExample {
  id: string;
  title: string;
  description: string;
  heading: string;
  goal: string;
  lastmod: string;
  summary: string;
  ideas: {title: string; statement: string}[];
}

function isPublicIdea(value: unknown): value is PublicExample['ideas'][number] {
  if (value === null || typeof value !== 'object' || Array.isArray(value))
    return false;
  const idea = value as Record<string, unknown>;
  return typeof idea.title === 'string' && typeof idea.statement === 'string';
}

function isPublicExample(value: unknown): value is PublicExample {
  if (value === null || typeof value !== 'object' || Array.isArray(value))
    return false;
  const example = value as Record<string, unknown>;
  return (
    typeof example.id === 'string' &&
    typeof example.title === 'string' &&
    typeof example.description === 'string' &&
    typeof example.heading === 'string' &&
    typeof example.goal === 'string' &&
    typeof example.lastmod === 'string' &&
    typeof example.summary === 'string' &&
    Array.isArray(example.ideas) &&
    example.ideas.length === 3 &&
    example.ideas.every(isPublicIdea)
  );
}

export async function listPublicExamples(): Promise<PublicExample[]> {
  const response = await fetch('/examples.json');
  if (!response.ok) throw new Error('Public examples could not be loaded');
  const examples: unknown = await response.json();
  if (!Array.isArray(examples) || !examples.every(isPublicExample))
    throw new Error('Public examples are unavailable');
  return examples;
}
