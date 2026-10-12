import {useState, useEffect, useMemo, useRef} from 'react';
import {joinClasses} from '@/shared/ui/classes';
import {
  LANDING_AGENTS,
  type LandingAgent,
  INITIAL_ELO,
  expectedScore,
  simulateEloHistory,
} from './home_landing_content';
import {type MotionProps, useInView} from './home_landing_hooks';

// Layout follows the architecture figure in Gottweis et al., Towards an AI co-
// scientist (2025).

// Specialists follow the paper's two-column grid.
const GRID: readonly (readonly [string, string])[] = [
  ['Generation', 'Proximity'],
  ['Reflection', 'Meta-review'],
  ['Ranking', 'Evolution'],
];
const SPECIALISTS = LANDING_AGENTS.slice(1);
const COLUMN_X = [280, 560];
const ROW_Y = [214, 314, 414];
const CELL_W = 240;
const CELL_H = 56;

function agentByName(name: string): LandingAgent {
  return SPECIALISTS.find(agent => agent.name === name) ?? SPECIALISTS[0];
}

interface NodeProps {
  x: number;
  y: number;
  w: number;
  h: number;
  className?: string;
  lines: string[];
}

function DiagramNode({x, y, w, h, className, lines}: NodeProps) {
  const cy = y + h / 2 - (lines.length - 1) * 10 + 5;
  return (
    <g className={joinClasses('ucs-landing-node', className)}>
      <rect x={x} y={y} width={w} height={h} rx={Math.min(h / 2, 22)} />
      {lines.map((line, i) => (
        <text key={line} x={x + w / 2} y={cy + i * 20} textAnchor="middle">
          {line}
        </text>
      ))}
    </g>
  );
}

function AgentCell({
  agent,
  x,
  y,
  active,
  onHold,
}: {
  agent: LandingAgent;
  x: number;
  y: number;
  active: boolean;
  onHold: (name: string | null) => void;
}) {
  return (
    <g
      className={joinClasses(
        'ucs-landing-agent-cell',
        `tone-${agent.tone}`,
        active && 'is-active',
      )}
      tabIndex={0}
      aria-label={`${agent.name} agent: ${agent.summary}`}
      onPointerEnter={() => onHold(agent.name)}
      onPointerLeave={() => onHold(null)}
      onFocus={() => onHold(agent.name)}
      onBlur={() => onHold(null)}
    >
      <rect x={x} y={y} width={CELL_W} height={CELL_H} rx={CELL_H / 2} />
      <text x={x + CELL_W / 2} y={y + CELL_H / 2 + 6} textAnchor="middle">
        {agent.name} agent
      </text>
    </g>
  );
}

// Crossing links follow the paper's lower-row exchanges.
const INNER_WIRES = [
  'M400 270 V310',
  'M400 370 V410',
  'M680 270 V310',
  'M680 370 V410',
  'M524 242 H556',
  'M524 342 H556',
  'M524 442 H556',
  'M562 316 L524 274',
  'M522 372 L558 412',
  'M558 372 L522 412',
];

const TWO_WAY = new Set(INNER_WIRES.filter(d => d.endsWith(' H556')));

const OUTER_WIRES = [
  'M100 88 H136',
  'M310 88 H346',
  'M516 88 H552',
  'M671 118 V186',
  'M786 88 H942',
  'M830 273 H876',
  'M830 440 H876',
  'M880 470 H834',
  'M1000 210 V150 H740 V122',
  'M1053 54 V24 H64 V50',
  'M64 156 V296',
  'M196 326 H246',
];

function DiagramWires({wires}: {wires: readonly string[]}) {
  return (
    <>
      {wires.map(d => (
        <path
          key={d}
          className={joinClasses(
            'ucs-landing-wire',
            TWO_WAY.has(d) && 'is-two-way',
          )}
          d={d}
        />
      ))}
    </>
  );
}

function DiagramFrame() {
  return (
    <>
      <defs>
        <marker
          id="ucs-landing-arrow"
          viewBox="0 0 10 10"
          refX="8"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M0 0 L10 5 L0 10 z" />
        </marker>
      </defs>
      <rect
        className="ucs-landing-box"
        x="250"
        y="190"
        width="580"
        height="340"
        rx="32"
      />
      <text
        className="ucs-landing-box-label"
        x="540"
        y="508"
        textAnchor="middle"
      >
        Open Co-Scientist specialized agents
      </text>
      <text className="ucs-landing-wire-label" x="660" y="160" textAnchor="end">
        assigns agents to workers
      </text>
      <circle className="ucs-landing-person" cx="64" cy="88" r="34" />
      <path
        className="ucs-landing-person-glyph"
        d="M64 70 a10 10 0 1 1 0 20 a10 10 0 1 1 0 -20 M46 110 c4 -14 32 -14 36 0 z"
      />
      <text
        className="ucs-landing-caption-text"
        x="64"
        y="146"
        textAnchor="middle"
      >
        You
      </text>
    </>
  );
}

const NODES: readonly (readonly [
  number,
  number,
  number,
  number,
  string,
  string[],
])[] = [
  [140, 62, 170, 52, 'is-you', ['Research goal']],
  [350, 62, 166, 52, '', ['Configuration']],
  [556, 58, 230, 60, 'is-supervisor', ['Supervisor agent']],
  [946, 54, 214, 68, 'is-output', ['Research overview', 'and ranked ideas']],
  [20, 300, 176, 52, 'is-you', ['Your feedback']],
  [880, 214, 240, 34, 'is-worker', ['Worker']],
  [880, 256, 240, 34, 'is-worker', ['Worker']],
  [880, 298, 240, 34, 'is-worker', ['Worker']],
  [880, 340, 240, 34, 'is-worker', ['Worker']],
  [880, 420, 240, 70, 'is-worker', ['Context', 'memory']],
];

function DiagramNodes() {
  return (
    <>
      {NODES.map(([x, y, w, h, className, lines]) => (
        <DiagramNode
          key={`${x}-${y}`}
          x={x}
          y={y}
          w={w}
          h={h}
          className={className}
          lines={lines}
        />
      ))}
    </>
  );
}

function DiagramSvg({
  active,
  onHold,
}: {
  active: string | null;
  onHold: (name: string | null) => void;
}) {
  return (
    <svg
      className="ucs-landing-diagram-svg"
      viewBox="0 0 1180 540"
      role="group"
      aria-label="System diagram: you set a research goal and configuration; the Supervisor agent assigns the Generation, Reflection, Ranking, Evolution, Proximity and Meta-review agents to workers that share a context memory; the run returns a research overview with ranked ideas, and your feedback flows back in."
    >
      <DiagramFrame />
      <DiagramWires wires={OUTER_WIRES} />
      <DiagramWires wires={INNER_WIRES} />
      <DiagramNodes />
      {GRID.map((row, r) =>
        row.map((name, c) => (
          <AgentCell
            key={name}
            agent={agentByName(name)}
            x={COLUMN_X[c]}
            y={ROW_Y[r]}
            active={active === name}
            onHold={onHold}
          />
        )),
      )}
    </svg>
  );
}

function DiagramCaption({active}: {active: string | null}) {
  if (!active) {
    return (
      <figcaption className="ucs-landing-diagram-caption">
        <span>Hover or tap an agent to see what it does.</span>
      </figcaption>
    );
  }
  const agent = agentByName(active);
  return (
    <figcaption className="ucs-landing-diagram-caption">
      <b>{agent.name} agent</b>
      <span>{agent.summary}</span>
    </figcaption>
  );
}

export function LandingDiagram() {
  const [active, setActive] = useState<string | null>(null);
  const choose = (name: string | null) => {
    if (name) setActive(name);
  };
  return (
    <figure className="ucs-landing-diagram">
      <DiagramSvg active={active} onHold={choose} />
      <DiagramCaption active={active} />
    </figure>
  );
}

const IDEAS: readonly {title: string; strength: number}[] = [
  {title: 'Metformin sensitizes GBM stem cells', strength: 0.9},
  {title: 'Valproate reprograms tumor microglia', strength: 0.45},
  {title: 'Statins trigger ferroptosis in GBM', strength: 0.7},
  {title: 'Chloroquine blocks autophagy rescue', strength: 0.55},
  {title: 'Disulfiram–copper targets ALDH+ cells', strength: 0.65},
  {title: 'Itraconazole halts Hedgehog signaling', strength: 0.4},
  {title: 'Mebendazole disrupts tumor microtubules', strength: 0.6},
  {title: 'Propranolol dampens stress signaling', strength: 0.35},
];

const K = 32;
const ROUNDS = 3;
const LEAF_W = 400;
const ROW_H = 48;
const TOP = 26;
const ROUND_X = [LEAF_W + 110, LEAF_W + 260, LEAF_W + 410];
const CHAMP_X = LEAF_W + 470;

interface Match {
  a: number;
  b: number;
  winner: number;
  y: number;
}

interface Bracket {
  rounds: Match[][];
  elo: number[][];
  leafY: number[];
}

function playBracket(): Bracket {
  const leafY = IDEAS.map((_, i) => TOP + i * ROW_H + ROW_H / 2);
  const elo = [IDEAS.map(() => INITIAL_ELO)];
  let field = IDEAS.map((_, i) => ({idea: i, y: leafY[i]}));
  const rounds: Match[][] = [];
  for (let r = 0; r < ROUNDS; r++) {
    const next = elo[r].slice();
    const round = pairUp(field).map(([p, q]) => {
      const winner = IDEAS[p.idea].strength >= IDEAS[q.idea].strength ? p : q;
      const loser = winner === p ? q : p;
      const delta =
        K * (1 - expectedScore(next[winner.idea], next[loser.idea]));
      next[winner.idea] += delta;
      next[loser.idea] -= delta;
      return {a: p.idea, b: q.idea, winner: winner.idea, y: (p.y + q.y) / 2};
    });
    rounds.push(round);
    elo.push(next);
    field = round.map(m => ({idea: m.winner, y: m.y}));
  }
  return {rounds, elo, leafY};
}

function pairUp<T>(items: T[]): [T, T][] {
  const pairs: [T, T][] = [];
  for (let i = 0; i < items.length; i += 2)
    pairs.push([items[i], items[i + 1]]);
  return pairs;
}

function xOfRound(round: number): number {
  return round < 0 ? LEAF_W : ROUND_X[round];
}

function Connector({
  fromX,
  fromY,
  match,
  toX,
  won,
}: {
  fromX: number;
  fromY: number;
  match: Match;
  toX: number;
  won: boolean;
}) {
  const mid = fromX + (toX - fromX) / 2;
  return (
    <path
      className={joinClasses('ucs-landing-tree-wire', won && 'is-won')}
      d={`M${fromX} ${fromY} H${mid} V${match.y} H${toX - 14}`}
    />
  );
}

function RoundWires({
  bracket,
  round,
  step,
}: {
  bracket: Bracket;
  round: number;
  step: number;
}) {
  const fromY = (idea: number) =>
    round === 0
      ? bracket.leafY[idea]
      : (bracket.rounds[round - 1].find(m => m.winner === idea)?.y ?? 0);
  return (
    <>
      {bracket.rounds[round].flatMap(match =>
        // Draw the winner after the loser so its highlighted path remains
        // visible.
        [match.winner === match.a ? match.b : match.a, match.winner].map(
          idea => (
            <Connector
              key={`${round}-${idea}`}
              fromX={xOfRound(round - 1) + (round === 0 ? 0 : 14)}
              fromY={fromY(idea)}
              match={match}
              toX={xOfRound(round)}
              won={step > round && match.winner === idea}
            />
          ),
        ),
      )}
    </>
  );
}

function RoundNodes({
  bracket,
  round,
  step,
}: {
  bracket: Bracket;
  round: number;
  step: number;
}) {
  return (
    <>
      {bracket.rounds[round].map(match => (
        <g
          key={match.y}
          className={joinClasses(
            'ucs-landing-tree-node',
            step === round && 'is-live',
            step > round && 'is-done',
          )}
        >
          <circle cx={xOfRound(round)} cy={match.y} r="14" />
          <text
            x={xOfRound(round)}
            y={match.y}
            textAnchor="middle"
            dominantBaseline="central"
          >
            {step > round ? '' : 'vs'}
          </text>
        </g>
      ))}
    </>
  );
}

function Leaves({bracket, step}: {bracket: Bracket; step: number}) {
  const champion = bracket.rounds[ROUNDS - 1][0].winner;
  return (
    <>
      {IDEAS.map((idea, i) => (
        <g
          key={idea.title}
          className={joinClasses(
            'ucs-landing-tree-leaf',
            step === ROUNDS && i === champion && 'is-champion',
          )}
        >
          <rect
            x="0"
            y={bracket.leafY[i] - 19}
            width={LEAF_W}
            height="38"
            rx="20"
          />
          <text x="18" y={bracket.leafY[i] + 5}>
            {idea.title}
          </text>
          <text
            className="ucs-landing-tree-elo"
            x={LEAF_W - 16}
            y={bracket.leafY[i] + 5}
            textAnchor="end"
          >
            {Math.round(bracket.elo[step][i])}
          </text>
        </g>
      ))}
    </>
  );
}

function Champion({bracket, step}: {bracket: Bracket; step: number}) {
  const final = bracket.rounds[ROUNDS - 1][0];
  const shown = step === ROUNDS;
  return (
    <g className={joinClasses('ucs-landing-tree-champ', shown && 'is-shown')}>
      <path
        className="ucs-landing-tree-wire is-won"
        d={`M${xOfRound(ROUNDS - 1) + 14} ${final.y} H${CHAMP_X}`}
      />
      <rect x={CHAMP_X} y={final.y - 34} width="190" height="68" rx="34" />
      <text x={CHAMP_X + 95} y={final.y - 4} textAnchor="middle">
        Top ranked
      </text>
      <text
        className="ucs-landing-tree-elo"
        x={CHAMP_X + 95}
        y={final.y + 18}
        textAnchor="middle"
      >
        Elo {Math.round(bracket.elo[ROUNDS][final.winner])}
      </text>
    </g>
  );
}

const STEP_MS = 1600;

// Pause off screen; reduced motion shows the finished tree.
function useBracketStep(visible: boolean, reduceMotion: boolean): number {
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (!visible || reduceMotion) return;
    const timer = window.setInterval(
      () => setStep(s => (s + 1) % (ROUNDS + 3)),
      STEP_MS,
    );
    return () => window.clearInterval(timer);
  }, [visible, reduceMotion]);
  return reduceMotion ? ROUNDS : Math.min(step, ROUNDS);
}

export function LandingBracket({reduceMotion}: MotionProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const visible = useInView(ref, {once: false, threshold: 0.3});
  const bracket = useMemo(playBracket, []);
  const step = useBracketStep(visible, reduceMotion);
  const rounds = Array.from({length: ROUNDS}, (_, r) => r);
  return (
    <div ref={ref} className="ucs-landing-panel ucs-landing-tree">
      <div>
        <svg
          viewBox={`0 0 ${CHAMP_X + 200} ${TOP * 2 + IDEAS.length * ROW_H}`}
          role="img"
          aria-label="Tournament tree: eight example hypotheses debate in pairs over three rounds. Each debate moves both ideas' Elo ratings, and the idea that wins every debate ends ranked first."
        >
          {rounds.map(r => (
            <RoundWires key={r} bracket={bracket} round={r} step={step} />
          ))}
          <Champion bracket={bracket} step={step} />
          {rounds.map(r => (
            <RoundNodes key={r} bracket={bracket} round={r} step={step} />
          ))}
          <Leaves bracket={bracket} step={step} />
        </svg>
      </div>
    </div>
  );
}

const LO = 1100;
const HI = 1320;
const LEFT = 44;
const PLOT_W = 500;
const BASE_Y = 290;
const PLOT_H = 270;
const ELO_GRID = [1100, 1150, 1200, 1250, 1300];

function useChartGeometry() {
  return useMemo(() => {
    const {history, leader} = simulateEloHistory();
    const matches = history.length - 1;
    const x = (m: number) => LEFT + (m / matches) * PLOT_W;
    const y = (elo: number) => BASE_Y - ((elo - LO) / (HI - LO)) * PLOT_H;
    const lines = history[0].map((_, i) =>
      history.map((h, m) => `${x(m).toFixed(1)} ${y(h[i]).toFixed(1)}`),
    );
    const final = history[matches][leader];
    return {
      matches,
      leader,
      final,
      paths: lines.map(points => 'M' + points.join('L')),
      grid: ELO_GRID.map(elo => ({elo, y: y(elo)})),
      end: {x: x(matches), y: y(final)},
    };
  }, []);
}

export function LandingEloChart({reduceMotion}: MotionProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const seen = useInView(ref, {threshold: 0.35});
  const chart = useChartGeometry();
  // Draw the leader last so its line stays on top.
  const order = chart.paths
    .map((d, i) => ({d, i}))
    .sort(
      (a, b) => Number(a.i === chart.leader) - Number(b.i === chart.leader),
    );
  return (
    <div
      ref={ref}
      className={joinClasses(
        'ucs-landing-panel ucs-landing-elo',
        (seen || reduceMotion) && 'is-drawn',
      )}
    >
      <h3 className="ucs-landing-panel-title">Ratings over one tournament</h3>
      <svg
        viewBox="0 0 560 320"
        role="img"
        aria-label="Line chart of Elo ratings over 64 matches for 8 simulated ideas; one idea climbs clearly above the rest."
      >
        <g className="ucs-landing-elo-grid">
          {chart.grid.map(g => (
            <g key={g.elo}>
              <line x1={LEFT} x2={LEFT + PLOT_W} y1={g.y} y2={g.y} />
              <text x="0" y={g.y + 4}>
                {g.elo}
              </text>
            </g>
          ))}
          <text x={LEFT} y="314">
            0
          </text>
          <text x={LEFT + PLOT_W - 14} y="314">
            {chart.matches}
          </text>
        </g>
        {order.map(({d, i}) => (
          <path
            key={i}
            d={d}
            pathLength={1}
            className={joinClasses(
              'ucs-landing-elo-line',
              i === chart.leader && 'is-leader',
            )}
          />
        ))}
        <circle
          className="ucs-landing-elo-end"
          cx={chart.end.x}
          cy={chart.end.y}
          r="5"
        />
      </svg>
      <div className="ucs-landing-elo-legend">
        <span>Matches</span>
        <span>
          <b>{Math.round(chart.final)}</b> final Elo of the leader
        </span>
      </div>
    </div>
  );
}
