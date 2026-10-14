import {
  type MouseEvent,
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from 'react';
import {Link, useLocation} from 'react-router-dom';
import {createPortal} from 'react-dom';
import {smoothScrollToSection} from '@/shared/lib/smooth_scroll';
import {
  Button,
  buttonClasses,
  ExternalLink,
  SegmentedControl,
} from '@/shared/ui';
import helixArt from '@/assets/landing/helix.webp';
import {joinClasses} from '@/shared/ui/classes';
import {
  LANDING_SECTIONS,
  FAQ,
  LANDING_AGENTS,
  LANDING_SAFETY,
  LANDING_SOURCES,
  LANDING_VERDICTS,
  type LandingTone,
  DEFAULT_TIER,
  LANDING_TIERS,
  type LandingTier,
  MAX_POOL,
} from './home_landing_content';
import {
  scrollParent,
  shapePath,
  type MotionProps,
  type ShapeName,
  useShapeMorph,
} from './home_landing_hooks';
import {Icon, type IconName} from '@/shared/ui/icon';
import moleculeArt from '@/assets/landing/molecule.webp';
import podiumArt from '@/assets/landing/podium.webp';
import flaskArt from '@/assets/landing/flask.webp';
import {
  LandingBracket,
  LandingDiagram,
  LandingEloChart,
} from './home_landing_diagram';
import {SUGGESTIONS} from './chat_home_stage';
import {useReducedMotion} from '@/shared/hooks/use_media_query';

// Lazy-load the landing section so it cannot delay chat first paint.

export const LANDING_ID = 'landing';

const COMPOSER_SELECTOR = '.reference-composer textarea';

function scrollBehavior(reduceMotion: boolean): ScrollBehavior {
  return reduceMotion ? 'auto' : 'smooth';
}

// Scroll the home pane directly; scrollIntoView can shift clipped shell
// ancestors.
const HOME_SCROLLER = '.ucs-page--home';

const RAIL_OFFSET = 72;

const LANDING_WIDTH = 'mx-auto w-[min(100%_-_32px,1240px)]';

const LABEL_CLASSES =
  'm-0 font-(family-name:--l-body) text-[14px] font-medium text-(--l-muted)';

const H2_CLASSES =
  'm-0 font-(family-name:--l-display) text-[clamp(2.2rem,4.6vw,3.8rem)] leading-[1.05] font-normal tracking-[-0.01em] text-balance';

const LEDE_CLASSES =
  'm-[16px_0_0] max-w-[36rem] text-[1.15rem] leading-[1.55] text-(--l-muted)';

const SR_ONLY_CLASSES =
  'absolute size-px overflow-hidden whitespace-nowrap [clip-path:inset(50%)]';

const RAIL_CLASSES =
  'ucs-landing-rail flex justify-center border-b border-b-(--l-line) bg-(--l-bg) px-4 py-2.5 phone:justify-start [.ucs-landing-header-tabs_&]:border-transparent [.ucs-landing-header-tabs_&]:bg-transparent';

const RAIL_NAV_CLASSES =
  'relative flex max-w-[min(100%,var(--rail-room,100%))] min-w-0 gap-1 overflow-x-auto rounded-full bg-(--l-surface) p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden [.ucs-landing-header-tabs:not([inert])_&]:pointer-events-auto';

const RAIL_TAB_CLASSES =
  'relative z-1 grid h-[38px] flex-none place-items-center rounded-full px-4 text-[14px] font-medium no-underline';

const CARD_H3_CLASSES = 'm-0 font-(family-name:--l-display) font-normal';

const FEATURE_P_CLASSES = 'leading-[1.5] text-(--l-muted)';

const OV_ITEM_CLASSES = 'bg-(--l-surface) px-5 py-3.5';

const OV_PANEL_CLASSES =
  'm-0 flex list-none flex-col gap-1.5 rounded-3xl bg-(--l-bg) p-3';

const SEC_CLASSES = 'scroll-mt-[72px] py-[clamp(44px,5.5vw,76px)]';

const CHIPS_CLASSES = 'flex flex-wrap items-center gap-2';

export function scrollToLandingSection(id: string, reduceMotion: boolean) {
  smoothScrollToSection(
    id,
    RAIL_OFFSET,
    HOME_SCROLLER,
    scrollBehavior(reduceMotion),
  );
}

function useStartResearch(
  rootRef: RefObject<HTMLElement | null>,
  reduceMotion: boolean,
) {
  return useCallback(() => {
    scrollParent(rootRef.current).scrollTo({
      top: 0,
      behavior: scrollBehavior(reduceMotion),
    });
    window.setTimeout(
      () =>
        document
          .querySelector<HTMLTextAreaElement>(COMPOSER_SELECTOR)
          ?.focus({preventScroll: true}),
      reduceMotion ? 0 : 600,
    );
  }, [rootRef, reduceMotion]);
}

function LandingHero({
  onStart,
  reduceMotion,
}: {
  onStart: () => void;
  reduceMotion: boolean;
}) {
  const cookie = shapePath('cookie12');
  return (
    <section
      className={`${LANDING_WIDTH} border-t border-t-(--l-line) pt-18 pb-10`}
      aria-labelledby="ucs-landing-word"
    >
      <h2
        id="ucs-landing-word"
        className="relative z-1 m-0 font-(family-name:--l-display) text-[clamp(3rem,7.5vw,8rem)] leading-[0.95] font-normal tracking-[-0.015em] text-balance"
      >
        Open Co-Scientist
      </h2>
      <div className="mt-8 grid grid-cols-[6fr_5fr] [align-items:start] gap-8 [@media(max-width:900px)]:mt-4 [@media(max-width:900px)]:grid-cols-[1fr]">
        <div className="grid gap-7 pt-2">
          <p className="m-0 font-(family-name:--l-display) text-[clamp(1.4rem,2.2vw,2rem)] leading-[1.25] tracking-[-0.01em]">
            A multi-agent partner for scientific discovery.
          </p>
          <div className="flex flex-wrap gap-3">
            <Button size="lg" onClick={onStart}>
              Start a research goal
            </Button>
            <Button
              variant="outlined"
              size="lg"
              onClick={() =>
                scrollToLandingSection('landing-how', reduceMotion)
              }
            >
              See how it works
            </Button>
            <ExternalLink
              href="https://github.com/guy915/Open-Co-Scientist"
              className={buttonClasses({variant: 'outlined', size: 'lg'})}
            >
              GitHub
            </ExternalLink>
          </div>
          <iframe
            className="aspect-[16/9] w-full rounded-xl border-0"
            src="https://www.youtube-nocookie.com/embed/Wnhe8a8kKc0"
            title="Open Co-Scientist trailer"
            loading="lazy"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
            allowFullScreen
          />
        </div>
        <svg
          className="tone-teal w-[min(100%,440px)] [align-self:center] [justify-self:end] overflow-visible [&>path]:fill-(--tone-c) [@media(max-width:900px)]:justify-self-center"
          viewBox="0 0 100 100"
          role="img"
          aria-label="3D render of a DNA double helix whose base pairs are blue, red, yellow and green, inside a scalloped cookie shape."
        >
          <defs>
            <clipPath id="ucs-landing-cookie">
              <path className="ucs-landing-spin" d={cookie} />
            </clipPath>
          </defs>
          <path className="ucs-landing-spin" d={cookie} />
          <image
            href={helixArt}
            x="6"
            y="6"
            width="88"
            height="88"
            clipPath="url(#ucs-landing-cookie)"
            preserveAspectRatio="xMidYMid meet"
          />
        </svg>
      </div>
    </section>
  );
}

function useActiveSection(): string {
  const [active, setActive] = useState(LANDING_SECTIONS[0].id);
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const sections = LANDING_SECTIONS.flatMap(({id}) => {
      const section = document.getElementById(id);
      return section ? [section] : [];
    });
    // Native observer entries can lag a smooth scroll. Read current geometry
    // on notifications and reconcile once more when the pane finishes moving.
    const update = () => {
      const top = window.innerHeight * 0.3;
      const bottom = window.innerHeight * 0.35;
      const hit = sections.find(section => {
        const rect = section.getBoundingClientRect();
        return rect.top <= bottom && rect.bottom >= top;
      });
      if (hit) setActive(hit.id);
    };
    const observer = new IntersectionObserver(update, {
      rootMargin: '-30% 0px -65% 0px',
    });
    for (const section of sections) observer.observe(section);
    const pane = sections[0]?.closest(HOME_SCROLLER);
    pane?.addEventListener('scrollend', update);
    return () => {
      observer.disconnect();
      pane?.removeEventListener('scrollend', update);
    };
  }, []);
  return active;
}

function centerTab(nav: HTMLElement, id: string, reduceMotion: boolean) {
  const tab = nav.querySelector<HTMLElement>(`[data-section="${id}"]`);
  if (!tab || nav.scrollWidth <= nav.clientWidth) return;
  const left = tab.offsetLeft - (nav.clientWidth - tab.offsetWidth) / 2;
  nav.scrollTo?.({left, behavior: scrollBehavior(reduceMotion)});
}

function useRailScroll(
  navRef: RefObject<HTMLElement | null>,
  active: string,
  reduceMotion: boolean,
) {
  const [more, setMore] = useState(false);
  useEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const update = () =>
      setMore(nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 4);
    update();
    nav.addEventListener('scroll', update, {passive: true});
    window.addEventListener('resize', update);
    return () => {
      nav.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, [navRef]);
  useEffect(() => {
    if (navRef.current) centerTab(navRef.current, active, reduceMotion);
  }, [navRef, active, reduceMotion]);
  return more;
}

// Width the header leaves free around its centre, so centred tabs never
// cover the lockup or the actions. The title slot is empty on the landing.
function headerRoom(header: HTMLElement, slot: HTMLElement): number {
  const box = header.getBoundingClientRect();
  const centre = box.left + box.width / 2;
  let left = box.left;
  let right = box.right;
  for (const child of Array.from(header.children)) {
    if (child === slot || child.classList.contains('ucs-header-title'))
      continue;
    const r = child.getBoundingClientRect();
    if (!r.width) continue;
    if (r.left + r.width / 2 < centre) left = Math.max(left, r.right);
    else right = Math.min(right, r.left);
  }
  return 2 * Math.min(centre - left, right - centre) - 24;
}

// A layer offset by a fraction of a device pixel is resampled, which draws a
// stray hairline over the docked labels.
function devicePixel(value: number): number {
  const ratio = window.devicePixelRatio || 1;
  return Math.round(value * ratio) / ratio;
}

// Below this the centred tabs would show barely one label; the page copy then
// sticks under the header instead.
const MIN_HEADER_ROOM = 320;

// The page copy scrolls natively; the header copy sits at the same spot and
// is clipped to the header, so the tabs slide continuously out of the pane
// into the header row instead of being re-parented mid-scroll.
function LandingRail({reduceMotion}: {reduceMotion: boolean}) {
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const copyRef = useRef<HTMLDivElement | null>(null);
  const focusedSectionRef = useRef<string | null>(null);
  const headerVisibleRef = useRef(false);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const [joined, setJoined] = useState(false);
  const [sticky, setSticky] = useState(false);
  useEffect(() => {
    const anchor = anchorRef.current;
    const pane = anchor?.closest('.ucs-page--home');
    const target = document.getElementById('header-landing-tabs');
    const header = target?.parentElement;
    if (!anchor || !pane || !target || !header) return;
    const update = () => {
      const fit = headerRoom(header, target);
      const narrow = fit < MIN_HEADER_ROOM;
      const a = anchor.getBoundingClientRect();
      const nextInHeader = !narrow && a.top < pane.getBoundingClientRect().top;
      if (nextInHeader !== headerVisibleRef.current) {
        const focused = document.activeElement;
        if (
          focused instanceof HTMLAnchorElement &&
          (anchor.contains(focused) || copyRef.current?.contains(focused))
        ) {
          focusedSectionRef.current = focused.dataset.section ?? null;
        }
        headerVisibleRef.current = nextInHeader;
      }
      setSticky(narrow);
      setSlot(narrow ? null : target);
      if (narrow) {
        anchor.style.removeProperty('--rail-room');
        return;
      }
      const h = header.getBoundingClientRect();
      anchor.style.setProperty('--rail-room', `${fit}px`);
      target.style.setProperty('--rail-room', `${fit}px`);
      const copy = copyRef.current;
      if (copy) {
        const rest = (h.height - copy.offsetHeight) / 2;
        copy.style.left = `${devicePixel(a.left - h.left)}px`;
        copy.style.width = `${a.width}px`;
        const y = devicePixel(Math.max(rest, a.top - h.top));
        copy.style.transform = `translateY(${y}px)`;
      }
      setJoined(nextInHeader);
    };
    update();
    const frame = requestAnimationFrame(update);
    pane.addEventListener('scroll', update, {passive: true});
    window.addEventListener('resize', update);
    return () => {
      cancelAnimationFrame(frame);
      pane.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, []);
  const inHeader = joined && slot !== null;
  useLayoutEffect(() => {
    const section = focusedSectionRef.current;
    if (!section) return;
    focusedSectionRef.current = null;
    const focused = document.activeElement;
    if (
      focused &&
      focused !== document.body &&
      !anchorRef.current?.contains(focused) &&
      !copyRef.current?.contains(focused)
    )
      return;
    const visible = inHeader ? copyRef.current : anchorRef.current;
    visible
      ?.querySelector<HTMLAnchorElement>(`a[data-section="${section}"]`)
      ?.focus({preventScroll: true});
  }, [inHeader, slot]);
  return (
    <div
      ref={anchorRef}
      className={joinClasses(
        'flex min-h-[66px] items-center justify-center gap-2',
        sticky && 'sticky top-0 z-5',
      )}
    >
      <div inert={inHeader} aria-hidden={inHeader || undefined}>
        <LandingTabs reduceMotion={reduceMotion} />
      </div>
      <Link
        to="/privacy"
        className={buttonClasses({variant: 'text', size: 'sm'})}
      >
        Privacy
      </Link>
      {slot &&
        createPortal(
          <div
            ref={copyRef}
            className="ucs-landing ucs-landing-header-tabs absolute top-0 left-0 [transform:translateY(-200%)]"
            inert={!inHeader}
            aria-hidden={!inHeader || undefined}
          >
            <LandingTabs reduceMotion={reduceMotion} />
          </div>,
          slot,
        )}
    </div>
  );
}

function LandingTabs({reduceMotion}: {reduceMotion: boolean}) {
  const active = useActiveSection();
  const navRef = useRef<HTMLElement | null>(null);
  const more = useRailScroll(navRef, active, reduceMotion);
  const go = (e: MouseEvent<HTMLAnchorElement>, id: string) => {
    e.preventDefault();
    scrollToLandingSection(id, reduceMotion);
  };
  return (
    <div className={RAIL_CLASSES}>
      <nav
        ref={navRef}
        aria-label="Landing sections"
        className={joinClasses(
          RAIL_NAV_CLASSES,
          more &&
            'phone:[mask-image:linear-gradient(90deg,#000_85%,transparent)]',
        )}
      >
        {LANDING_SECTIONS.map(({id, label}) => (
          <a
            key={id}
            href={`#${id}`}
            data-section={id}
            className={joinClasses(
              RAIL_TAB_CLASSES,
              active === id
                ? 'is-active bg-(--l-ink) text-(--l-bg)'
                : 'text-(--l-muted)',
            )}
            aria-current={active === id ? 'true' : undefined}
            onClick={e => go(e, id)}
          >
            {label}
          </a>
        ))}
      </nav>
    </div>
  );
}

function useHashLanding(reduceMotion: boolean) {
  const {hash, key} = useLocation();
  useEffect(() => {
    const id = hash.slice(1);
    if (!LANDING_SECTIONS.some(section => section.id === id)) return;
    const timer = window.setTimeout(
      () => scrollToLandingSection(id, reduceMotion),
      50,
    );
    return () => window.clearTimeout(timer);
  }, [hash, key, reduceMotion]);
}

export default function HomeLanding() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const reduceMotion = useReducedMotion();
  const onStart = useStartResearch(rootRef, reduceMotion);
  useHashLanding(reduceMotion);
  return (
    <div ref={rootRef} id={LANDING_ID} className="ucs-landing">
      <LandingHero onStart={onStart} reduceMotion={reduceMotion} />
      <LandingRail reduceMotion={reduceMotion} />
      <div className={LANDING_WIDTH}>
        <OverviewSection />
        <HowSection reduceMotion={reduceMotion} />
        <TournamentSection reduceMotion={reduceMotion} />
        <EvidenceSection />
        <SafetySection reduceMotion={reduceMotion} />
        <TiersSection reduceMotion={reduceMotion} />
        <ClosingSection onStart={onStart} />
        <FaqSection />
      </div>
    </div>
  );
}

function SectionHeading({title, lede}: {title: string; lede?: string}) {
  return (
    <>
      <h2 className={H2_CLASSES}>{title}</h2>
      {lede && <p className={LEDE_CLASSES}>{lede}</p>}
    </>
  );
}

function ShapeBadge({
  shape,
  tone,
  icon,
  pathRef,
  compact,
}: {
  shape: ShapeName;
  tone: LandingTone;
  icon: IconName;
  pathRef?: Ref<SVGPathElement>;
  compact?: boolean;
}) {
  return (
    <span
      className={joinClasses(
        'relative block text-(--tone-o)',
        compact ? 'w-[88px]' : 'w-[104px]',
        `tone-${tone}`,
      )}
    >
      <svg
        viewBox="0 0 100 100"
        aria-hidden="true"
        className="block h-auto w-full overflow-visible [&_path]:fill-(--tone-c)"
      >
        <path ref={pathRef} d={shapePath(shape)} />
      </svg>
      <Icon
        name={icon}
        className={joinClasses(
          'absolute inset-0 m-auto',
          compact ? 'h-[34px] w-[34px]' : 'h-[40px] w-[40px]',
        )}
      />
    </span>
  );
}

function AgentCard({
  agent,
  wide,
  reduceMotion,
}: {
  agent: (typeof LANDING_AGENTS)[number];
  wide: boolean;
  reduceMotion: boolean;
}) {
  const morph = useShapeMorph(agent.shape, reduceMotion);
  return (
    <article
      className={joinClasses(
        'grid grid-cols-[104px_1fr] items-center gap-6 rounded-4xl bg-(--l-surface) p-7 outline-offset-[3px]',
        wide && '[grid-column:span_3] [@media(max-width:900px)]:col-auto',
      )}
      tabIndex={0}
      onPointerEnter={morph.toCircle}
      onPointerLeave={morph.toRest}
      onFocus={morph.toCircle}
      onBlur={morph.toRest}
    >
      <ShapeBadge
        shape={agent.shape}
        tone={agent.tone}
        icon={agent.icon}
        pathRef={morph.pathRef}
      />
      <div>
        <h3 className={`${CARD_H3_CLASSES} text-[28px] tracking-[-0.01em]`}>
          {agent.name}
        </h3>
        <p className={`${FEATURE_P_CLASSES} m-[6px_0_0]`}>{agent.summary}</p>
      </div>
    </article>
  );
}

export function HowSection({reduceMotion}: MotionProps) {
  return (
    <section className={SEC_CLASSES} id="landing-how">
      <SectionHeading
        title="How it works"
        lede="A Supervisor plans the run and hands work to six specialist agents. The loop repeats until the rankings settle, then you get a research overview back."
      />
      <LandingDiagram />
      <div className="mt-4 grid grid-cols-[repeat(3,1fr)] gap-4 [@media(max-width:900px)]:grid-cols-[1fr]">
        {LANDING_AGENTS.map((agent, i) => (
          <AgentCard
            key={agent.name}
            agent={agent}
            wide={i === 0}
            reduceMotion={reduceMotion}
          />
        ))}
      </div>
    </section>
  );
}

export function TournamentSection({reduceMotion}: MotionProps) {
  return (
    <section className={SEC_CLASSES} id="landing-tournament">
      <SectionHeading
        title="Tournament"
        lede="Ideas meet in pairwise debates. Every win and loss moves their Elo rating, so the ranking reflects many arguments, not one score."
      />
      <LandingBracket reduceMotion={reduceMotion} />
      <div className="mt-4 grid grid-cols-[7fr_5fr] gap-4 [@media(max-width:900px)]:grid-cols-[1fr]">
        <LandingEloChart reduceMotion={reduceMotion} />
        <div className="relative grid [align-content:space-between] gap-4 overflow-hidden rounded-4xl bg-(--l-c-blue) p-8 text-(--l-o-blue)">
          <div>
            <h3 className={`${CARD_H3_CLASSES} text-[28px]`}>Ranked ideas</h3>
            <p className="m-[8px_0_0] max-w-[26rem] leading-[1.5] opacity-80">
              Every hypothesis comes back with its Elo rating, its reviews, and
              the ideas it was bred from.
            </p>
          </div>
          <img
            src={podiumArt}
            width={1300}
            height={1027}
            className="block h-auto w-[min(100%,420px)] justify-self-center"
            loading="lazy"
            decoding="async"
            alt="3D render: seven pedestals of falling height, the top three crowned with blue, teal and yellow spheres."
          />
        </div>
      </div>
    </section>
  );
}

function SourcesMarquee() {
  const items = [...LANDING_SOURCES, ...LANDING_SOURCES];
  return (
    <div className="mt-16">
      <p className={LABEL_CLASSES}>Built on the literature</p>
      <div
        className="ucs-landing-marquee-window mt-4 overflow-hidden border-y border-y-(--l-line) py-6 [mask-image:linear-gradient(90deg,transparent,#000_8%,#000_92%,transparent)]"
        aria-hidden="true"
      >
        <div className="flex w-max gap-14 motion-safe:[animation:ucs-landing-slide_40s_linear_infinite] motion-safe:[.ucs-landing-marquee-window:hover_&]:[animation-play-state:paused]">
          {items.map((source, i) => (
            <span
              key={i}
              className="font-(family-name:--l-display) text-[clamp(2.4rem,5.6vw,4.8rem)] tracking-[-0.01em] whitespace-nowrap text-(--l-ink)"
            >
              {source}
            </span>
          ))}
        </div>
      </div>
      <p className={SR_ONLY_CLASSES}>Sources: {LANDING_SOURCES.join(', ')}.</p>
    </div>
  );
}

export function EvidenceSection() {
  return (
    <section className={SEC_CLASSES} id="landing-evidence">
      <SectionHeading title="Evidence" />
      <div className="mt-10 grid grid-cols-[4fr_7fr] items-center gap-12 [@media(max-width:900px)]:grid-cols-[1fr]">
        <svg
          className="tone-green block h-auto w-full overflow-visible [&>path]:fill-(--tone-c)"
          viewBox="0 0 100 100"
          role="img"
          aria-label="3D render of a small-molecule drug inside a flower shape."
        >
          <defs>
            <clipPath id="ucs-landing-flower">
              <path d={shapePath('flower')} />
            </clipPath>
          </defs>
          <path d={shapePath('flower')} />
          <image
            href={moleculeArt}
            x="14"
            y="30"
            width="72"
            height="40"
            clipPath="url(#ucs-landing-flower)"
            preserveAspectRatio="xMidYMid meet"
          />
        </svg>
        <div>
          <p className={LEDE_CLASSES}>
            Each idea the report features is split into atomic claims. Passages
            from the literature are matched to every claim and judged.
          </p>
          <div className="mt-7 grid gap-3">
            {LANDING_VERDICTS.map(v => (
              <div
                key={v.label}
                className={`tone-${v.tone} grid grid-cols-[120px_1fr] items-center gap-4 rounded-3xl bg-(--tone-c) px-6 py-5 text-[15px] leading-[1.45] text-(--tone-o) [@media(max-width:900px)]:grid-cols-[1fr] [@media(max-width:900px)]:gap-1`}
              >
                <b className="font-medium">{v.label}</b>
                <span>{v.body}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      <SourcesMarquee />
    </section>
  );
}

function SafetyCard({
  layer,
  reduceMotion,
}: {
  layer: (typeof LANDING_SAFETY)[number];
  reduceMotion: boolean;
}) {
  const morph = useShapeMorph(layer.shape, reduceMotion);
  return (
    <article
      className="grid [align-content:start] gap-3 rounded-4xl border border-(--l-line) p-7"
      tabIndex={0}
      onPointerEnter={morph.toCircle}
      onPointerLeave={morph.toRest}
      onFocus={morph.toCircle}
      onBlur={morph.toRest}
    >
      <ShapeBadge
        shape={layer.shape}
        tone={layer.tone}
        icon={layer.icon}
        pathRef={morph.pathRef}
        compact
      />
      <h3 className={`${CARD_H3_CLASSES} text-[24px]`}>{layer.title}</h3>
      <p className={`${FEATURE_P_CLASSES} m-0`}>{layer.body}</p>
    </article>
  );
}

export function SafetySection({reduceMotion}: MotionProps) {
  return (
    <section className={SEC_CLASSES} id="landing-safety">
      <SectionHeading title="Safety" />
      <div className="mt-12 grid grid-cols-[repeat(3,1fr)] gap-4 [@media(max-width:900px)]:grid-cols-[1fr]">
        {LANDING_SAFETY.map(layer => (
          <SafetyCard
            key={layer.title}
            layer={layer}
            reduceMotion={reduceMotion}
          />
        ))}
      </div>
    </section>
  );
}

export function TiersSection({reduceMotion}: MotionProps) {
  return (
    <section className={SEC_CLASSES} id="landing-tiers">
      <SectionHeading
        title="Tiers"
        lede="Choose a tier when you start. It sets how many ideas are seeded, how many evolution cycles run, and how large the pool can grow."
      />
      <LandingTiers reduceMotion={reduceMotion} />
    </section>
  );
}

export function FaqSection() {
  return (
    <section className={SEC_CLASSES} id="faq">
      <SectionHeading title="Questions" />
      <div className="mt-8 grid">
        {FAQ.map(entry => (
          <details
            key={entry.question}
            className="border-b border-b-(--l-line)"
          >
            <summary className="flex cursor-pointer items-center justify-between gap-4 py-6 text-[19px] [list-style:none] [&::-webkit-details-marker]:hidden">
              <span>{entry.question}</span>
              <Icon
                name="expand_more"
                className="size-[24px] flex-none fill-(--l-muted) [transition:transform_0.3s_var(--l-ease)] [details[open]_&]:[transform:rotate(180deg)]"
              />
            </summary>
            <p className="m-[0_0_22px] max-w-[60rem] leading-[1.6] text-(--l-muted)">
              {entry.answer}
            </p>
          </details>
        ))}
      </div>
    </section>
  );
}

export function ClosingSection({onStart}: {onStart: () => void}): ReactNode {
  return (
    <section className="scroll-mt-[72px] pt-0 pb-[clamp(44px,5.5vw,76px)]">
      <div className="grid grid-cols-[1fr_340px] items-center gap-8 rounded-5xl bg-(--l-c-teal) p-[clamp(28px,5vw,72px)] text-(--l-o-teal) [@media(max-width:900px)]:grid-cols-[1fr]">
        <div>
          <h2 className={H2_CLASSES}>Start with a question.</h2>
          <p className="m-[16px_0_28px] max-w-[30rem] text-[1.15rem] leading-[1.55] opacity-80">
            Describe what you want to find out.{' '}
            <span className="whitespace-nowrap">Open Co-Scientist</span> asks
            what a strong answer needs, then sends its agents to work.
          </p>
          <Button variant="tonal" size="lg" onClick={onStart}>
            Start a research goal
          </Button>
        </div>
        <img
          src={flaskArt}
          width={774}
          height={1069}
          className="block h-auto max-h-[440px] w-full justify-self-center object-contain"
          loading="lazy"
          decoding="async"
          alt="3D render of a glass Erlenmeyer flask holding a glowing teal liquid."
        />
      </div>
    </section>
  );
}

const STAGES: readonly {icon: IconName; label: string}[] = [
  {icon: 'menu_book', label: 'Reads the literature on your goal'},
  {icon: 'lightbulb', label: 'Writes candidate hypotheses'},
  {
    icon: 'rate_review',
    label: 'Reviews each for correctness, novelty and testability',
  },
  {icon: 'leaderboard', label: 'Ranks them in an Elo tournament of debates'},
  {icon: 'genetics', label: 'Evolves the strongest into better ideas'},
  {icon: 'summarize', label: 'Writes a research overview'},
];

const [GOAL_TITLE, GOAL_BODY] = SUGGESTIONS[0].prompt.split('\n\n');

const RANKED: readonly {title: string; elo: number}[] = [
  {
    title: 'Metformin sensitizes glioblastoma stem cells to temozolomide',
    elo: 1287,
  },
  {title: 'Statins trigger ferroptosis in GBM', elo: 1241},
  {title: 'Disulfiram–copper targets ALDH+ cells', elo: 1226},
];

function OverviewCard({
  title,
  sub,
  children,
}: {
  title: string;
  sub: string;
  children: ReactNode;
}) {
  return (
    <div className="ucs-landing-ov-card grid [grid-row:span_3] grid-rows-subgrid gap-2 rounded-4xl bg-(--l-surface) p-7">
      <h3 className={`${CARD_H3_CLASSES} text-[26px]`}>{title}</h3>
      <p className="m-[0_0_12px] text-[15px] leading-[1.5] text-(--l-muted)">
        {sub}
      </p>
      {children}
    </div>
  );
}

function Chip({tone, children}: {tone: LandingTone; children: string}) {
  return (
    <span
      className={`tone-${tone} rounded-full bg-(--tone-c) px-3 py-1 text-[13px] font-medium text-(--tone-o)`}
    >
      {children}
    </span>
  );
}

function InputCard() {
  return (
    <OverviewCard
      title="You write"
      sub="A research goal in plain language. Add your own papers and choose the sources to search if you like."
    >
      <div className={OV_PANEL_CLASSES}>
        <div className={`${OV_ITEM_CLASSES} flex-1 rounded-tile`}>
          <b className="mb-2 block font-(family-name:--l-display) text-[18px] leading-[1.35] font-normal">
            {GOAL_TITLE}
          </b>
          <p className="m-0 text-[15px] leading-[1.55] text-(--l-muted)">
            {GOAL_BODY}
          </p>
        </div>
        <div className={`${CHIPS_CLASSES} p-[6px_6px_2px]`}>
          <Chip tone="blue">PubMed</Chip>
          <Chip tone="blue">Europe PMC</Chip>
          <Chip tone="teal">Standard tier</Chip>
        </div>
      </div>
    </OverviewCard>
  );
}

function RunCard() {
  return (
    <OverviewCard
      title="The agents"
      sub="Work through the goal in a loop, as many cycles as the tier allows."
    >
      <ol className={OV_PANEL_CLASSES}>
        {STAGES.map(stage => (
          <li
            key={stage.label}
            className={`${OV_ITEM_CLASSES} flex flex-1 items-center gap-3 rounded-full text-[14px] leading-[1.35]`}
          >
            <Icon
              name={stage.icon}
              className="size-[20px] flex-none fill-(--l-accent)"
            />
            <span>{stage.label}</span>
          </li>
        ))}
      </ol>
    </OverviewCard>
  );
}

function OutputCard() {
  return (
    <OverviewCard
      title="You get"
      sub="Ranked hypotheses, each with its reviews, Elo rating, and claims checked against sources, plus a report you can share."
    >
      <ol className={OV_PANEL_CLASSES}>
        {RANKED.map((idea, i) => (
          <li
            key={idea.title}
            className={`${OV_ITEM_CLASSES} flex flex-col justify-center rounded-tile ${
              i === 0 ? 'gap-2.5 [flex:1.7_1_0%]' : 'flex-1 gap-1.5'
            }`}
          >
            <span className="text-[13px] text-(--l-muted) tabular-nums">
              #{i + 1} · Elo {idea.elo}
            </span>
            <b
              className={
                i === 0
                  ? 'font-(family-name:--l-display) text-[20px] leading-[1.3] font-normal'
                  : 'text-[15px] leading-[1.35] font-medium'
              }
            >
              {idea.title}
            </b>
            {i === 0 && (
              <div className={CHIPS_CLASSES}>
                <Chip tone="green">Supports 3</Chip>
                <Chip tone="yellow">Partial 1</Chip>
              </div>
            )}
          </li>
        ))}
      </ol>
    </OverviewCard>
  );
}

export function OverviewSection() {
  return (
    <section className={SEC_CLASSES} id="landing-overview">
      <h2 className={H2_CLASSES}>From a question to ranked ideas</h2>
      <p className={LEDE_CLASSES}>
        One run, end to end. The example uses the first suggestion on the home
        screen; the output shown is illustrative.
      </p>
      <div className="mt-10 grid grid-cols-[repeat(3,1fr)] gap-4 [@media(max-width:900px)]:grid-cols-[1fr]">
        <InputCard />
        <RunCard />
        <OutputCard />
      </div>
    </section>
  );
}

// The tier visualization mirrors product RUN_TIER_DEFAULTS rather than
// inventing pool sizes.

function dotGeneration(tier: LandingTier, index: number): number | null {
  if (index < tier.seeds) return 0;
  if (index >= tier.maxIdeas) return null;
  const perCycle = (tier.maxIdeas - tier.seeds) / tier.cycles;
  return Math.min(tier.cycles, 1 + Math.floor((index - tier.seeds) / perCycle));
}

const GENERATIONS = [
  {label: 'Seed ideas', generation: 0},
  {label: 'Cycle 1', generation: 1},
  {label: 'Cycle 2', generation: 2},
  {label: 'Cycle 3', generation: 3},
  {label: 'Cycle 4', generation: 4},
];

const DOT_CLASSES = [
  '[--dot:var(--l-muted)]',
  '[--dot:var(--l-dot-blue)]',
  '[--dot:var(--l-dot-green)]',
  '[--dot:var(--l-dot-yellow)]',
  '[--dot:var(--l-dot-red)]',
];

function TierField({tier, reduceMotion}: {tier: LandingTier} & MotionProps) {
  return (
    <div>
      <div
        className="grid grid-cols-[repeat(24,1fr)] gap-2 [@media(max-width:900px)]:grid-cols-[repeat(12,1fr)]"
        aria-hidden="true"
      >
        {Array.from({length: MAX_POOL}, (_, i) => {
          const generation = dotGeneration(tier, i);
          return (
            <i
              key={i}
              className={joinClasses(
                'aspect-square rounded-full [transition:transform_500ms_var(--l-ease),background-color_500ms] motion-reduce:[transition:none]',
                generation === null
                  ? 'bg-(--l-line) [transform:scale(0.35)]'
                  : `${DOT_CLASSES[generation]} bg-(--dot) [transform:scale(1)]`,
              )}
              style={{transitionDelay: reduceMotion ? '0ms' : `${i * 6}ms`}}
            />
          );
        })}
      </div>
      <div className="mt-5 flex flex-wrap gap-3.5 text-[13px] text-(--l-muted)">
        {GENERATIONS.filter(g => g.generation <= tier.cycles).map(g => (
          <span
            key={g.label}
            className={`${DOT_CLASSES[g.generation]} before:mr-1.5 before:inline-block before:size-[10px] before:rounded-full before:bg-(--dot) before:[vertical-align:-1px] before:content-['']`}
          >
            {g.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function TierStat({value, label}: {value: number; label: string}) {
  return (
    <div className="grid">
      <b className="font-(family-name:--l-display) text-[56px] leading-none font-normal tracking-[-0.01em] tabular-nums [@media(max-width:900px)]:text-[44px]">
        {value}
      </b>
      <span className="text-[14px] text-(--l-muted)">{label}</span>
    </div>
  );
}

function TierStats({tier}: {tier: LandingTier}) {
  return (
    <div
      className="grid gap-5 [@media(max-width:900px)]:grid-cols-[repeat(3,1fr)]"
      aria-live="polite"
    >
      <TierStat value={tier.maxIdeas} label="ideas at most" />
      <TierStat value={tier.seeds} label="seed ideas" />
      <TierStat
        value={tier.cycles}
        label={tier.cycles === 1 ? 'evolution cycle' : 'evolution cycles'}
      />
    </div>
  );
}

export function LandingTiers({reduceMotion}: MotionProps) {
  const [name, setName] = useState(DEFAULT_TIER);
  const tier = LANDING_TIERS.find(t => t.name === name) ?? LANDING_TIERS[1];
  return (
    <>
      <SegmentedControl
        label="Run tier"
        size="lg"
        value={name}
        onChange={setName}
        options={LANDING_TIERS.map(t => ({value: t.name, label: t.name}))}
        layoutClassName="mt-8"
      />
      <div className="mt-5 grid grid-cols-[1fr_260px] items-center gap-8 rounded-4xl bg-(--l-surface) p-[clamp(20px,3vw,40px)] [@media(max-width:900px)]:grid-cols-[1fr]">
        <TierField tier={tier} reduceMotion={reduceMotion} />
        <TierStats tier={tier} />
      </div>
    </>
  );
}
