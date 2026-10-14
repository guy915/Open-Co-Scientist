import type {IconName} from '@/shared/ui/icon';
import type {ShapeName} from './home_landing_hooks';

// Keep tier facts synchronized with engine RUN_TIER_DEFAULTS and initial Elo
// with engine INITIAL_ELO_RATING.

export type LandingTone = 'teal' | 'blue' | 'green' | 'yellow' | 'red';

export const INITIAL_ELO = 1200;

export interface LandingSection {
  id: string;
  label: string;
}

export const LANDING_SECTIONS: readonly LandingSection[] = [
  {id: 'landing-overview', label: 'Overview'},
  {id: 'landing-how', label: 'How it works'},
  {id: 'landing-tournament', label: 'Tournament'},
  {id: 'landing-evidence', label: 'Evidence'},
  {id: 'landing-safety', label: 'Safety'},
  {id: 'landing-tiers', label: 'Tiers'},
  {id: 'faq', label: 'FAQ'},
];

export interface LandingAgent {
  name: string;
  summary: string;
  icon: IconName;
  shape: ShapeName;
  tone: LandingTone;
}

export const LANDING_AGENTS: readonly LandingAgent[] = [
  {
    name: 'Supervisor',
    summary:
      'Reads your goal, writes the research plan, and decides which agent ' +
      'works next as the hypotheses develop.',
    icon: 'account_tree',
    shape: 'cookie12',
    tone: 'teal',
  },
  {
    name: 'Generation',
    summary:
      'Drafts new hypotheses from the literature and simulated scientific ' +
      'debate.',
    icon: 'lightbulb',
    shape: 'flower',
    tone: 'blue',
  },
  {
    name: 'Reflection',
    summary: 'Reviews each idea for correctness, novelty, and testability.',
    icon: 'rate_review',
    shape: 'gem',
    tone: 'red',
  },
  {
    name: 'Ranking',
    summary: 'Compares pairs of hypotheses and updates their Elo ratings.',
    icon: 'leaderboard',
    shape: 'pill',
    tone: 'yellow',
  },
  {
    name: 'Evolution',
    summary: 'Creates improved hypotheses from the strongest ideas.',
    icon: 'genetics',
    shape: 'clover',
    tone: 'green',
  },
  {
    name: 'Proximity',
    summary: 'Groups similar hypotheses so comparisons test real differences.',
    icon: 'join',
    shape: 'cookie7',
    tone: 'blue',
  },
  {
    name: 'Meta-review',
    summary: 'Summarizes the debates and writes the research overview.',
    icon: 'summarize',
    shape: 'sunny',
    tone: 'teal',
  },
];

export interface LandingSafetyLayer {
  title: string;
  body: string;
  icon: IconName;
  shape: ShapeName;
  tone: LandingTone;
}

export const LANDING_SAFETY: readonly LandingSafetyLayer[] = [
  {
    title: 'Goal screening',
    body: 'Every research goal is checked at intake, before any agent starts work.',
    icon: 'policy',
    shape: 'clover',
    tone: 'yellow',
  },
  {
    title: 'Idea screening',
    body: 'Each hypothesis is screened again before it can enter the tournament.',
    icon: 'verified_user',
    shape: 'sunny',
    tone: 'red',
  },
  {
    title: 'Human review',
    body: 'Anything held waits for a person to decide. Nothing held is published on its own.',
    icon: 'person',
    shape: 'cookie7',
    tone: 'blue',
  },
];

export interface LandingTier {
  name: string;
  seeds: number;
  cycles: number;
  maxIdeas: number;
}

export const LANDING_TIERS: readonly LandingTier[] = [
  {name: 'Express', seeds: 4, cycles: 1, maxIdeas: 12},
  {name: 'Standard', seeds: 8, cycles: 2, maxIdeas: 32},
  {name: 'Extended', seeds: 12, cycles: 3, maxIdeas: 60},
  {name: 'Ultra', seeds: 16, cycles: 4, maxIdeas: 96},
];

export const DEFAULT_TIER = 'Standard';

export const MAX_POOL = 96;

export const LANDING_SOURCES: readonly string[] = [
  'PubMed',
  'Europe PMC',
  'OpenAlex',
  'ChEMBL',
  'UniProt',
  'bioRxiv',
  'medRxiv',
  'The open web',
];

export interface LandingVerdict {
  label: string;
  body: string;
  tone: LandingTone;
}

export const LANDING_VERDICTS: readonly LandingVerdict[] = [
  {
    label: 'Supports',
    body: 'A passage states the claim or its direct mechanism.',
    tone: 'green',
  },
  {
    label: 'Partial',
    body: 'A passage supports part of the claim, or under narrower conditions.',
    tone: 'yellow',
  },
  {
    label: 'Contradicts',
    body: 'A passage on the same subject states the opposite.',
    tone: 'red',
  },
];

export interface FaqEntry {
  question: string;
  answer: string;
}

export const FAQ: readonly FaqEntry[] = [
  {
    question: 'What is Open Co-Scientist?',
    answer:
      'A research workspace that generates, reviews, and ranks hypotheses ' +
      'for your goal. Specialized agents propose ideas, review them, and ' +
      'compare pairs of hypotheses.',
  },
  {
    question: 'How do I start a run?',
    answer:
      'From the home screen, describe your research goal in the message box and ' +
      'send it. Open Co-Scientist confirms the setup, then the agents generate ' +
      'and evaluate ideas. Follow progress and results in the run view.',
  },
  {
    question: 'What does a run produce?',
    answer:
      'A ranked set of hypotheses, each with reviews and an Elo rating from ' +
      'the tournament. The leading ideas also have their claims checked ' +
      'against the literature. It also writes ' +
      'a research overview with open questions and draft specific aims.',
  },
  {
    question: 'Where does the evidence come from?',
    answer:
      'From literature tools such as PubMed, Europe PMC, preprint servers, ' +
      'OpenAlex, ChEMBL and UniProt, plus the web when search is on. ' +
      'Turn sources on or off from Connectors in the composer.',
  },
  {
    question: 'Are the results true?',
    answer:
      'They are hypotheses, not findings. Open Co-Scientist ranks ideas by ' +
      'argument and evidence so you can decide which ones deserve an ' +
      'experiment.',
  },
  {
    question: 'Do I need an API key?',
    answer:
      'No. Without a key you are on free usage: you can start Express runs ' +
      'only, up to 3 per day on this device. Add your own key under Model ' +
      'to use every run type, with higher daily limits.',
  },
  {
    question: 'Which model does it use?',
    answer:
      'Without an API key, runs use the free model configured for the ' +
      'deployment. When you add your own API key under Model, you choose a ' +
      'supervisor model (planning and the final report) and a worker model ' +
      '(generating, reviewing, and ranking ideas) from your provider.',
  },
  {
    question: 'Where does my API key go?',
    answer:
      'The key you enter under Model stays in page memory and is sent to ' +
      'the server when you start a run or chat with the Agent. The server ' +
      'checks it with the provider, then stores it encrypted for that run ' +
      'only and never returns it. Reloading or closing the page clears the ' +
      'browser copy. Existing background runs keep their encrypted credentials.',
  },
];

// Charts are labeled illustrations, but their rating updates use the real Elo
// rule.

export function expectedScore(a: number, b: number): number {
  return 1 / (1 + 10 ** ((b - a) / 400));
}

// Park-Miller seeding makes the illustration identical on every load.
function seededRandom(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 16807) % 2147483647;
    return state / 2147483647;
  };
}

export interface EloHistory {
  history: number[][];
  leader: number;
}

const CHART_K = 32;

function playChartMatch(
  elo: number[],
  skill: readonly number[],
  random: () => number,
) {
  const a = Math.floor(random() * elo.length);
  let b = Math.floor(random() * (elo.length - 1));
  if (b >= a) b++;
  const expected = expectedScore(elo[a], elo[b]);
  const aWins = random() < skill[a] / (skill[a] + skill[b]) ? 1 : 0;
  elo[a] += CHART_K * (aWins - expected);
  elo[b] -= CHART_K * (aWins - expected);
}

export function simulateEloHistory(
  ideas = 8,
  matches = 64,
  seed = 7,
): EloHistory {
  const random = seededRandom(seed);
  const skill = Array.from({length: ideas}, (_, i) =>
    i === 0 ? 0.92 : 0.3 + random() * 0.28,
  );
  const elo = Array<number>(ideas).fill(INITIAL_ELO);
  const history = [elo.slice()];
  for (let m = 0; m < matches; m++) {
    playChartMatch(elo, skill, random);
    history.push(elo.slice());
  }
  return {history, leader: elo.indexOf(Math.max(...elo))};
}
