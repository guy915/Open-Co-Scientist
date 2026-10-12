import {afterEach, expect, it, vi} from 'vitest';
import {afterFirstView} from './after_first_view';

const fontsDescriptor = Object.getOwnPropertyDescriptor(document, 'fonts');
const animationsDescriptor = Object.getOwnPropertyDescriptor(
  document,
  'getAnimations',
);

afterEach(() => {
  if (fontsDescriptor)
    Object.defineProperty(document, 'fonts', fontsDescriptor);
  else Reflect.deleteProperty(document, 'fonts');
  if (animationsDescriptor)
    Object.defineProperty(document, 'getAnimations', animationsDescriptor);
  else Reflect.deleteProperty(document, 'getAnimations');
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('waits for fonts and finite entry motion, ignores loops, and schedules idle work after paint', async () => {
  vi.useFakeTimers();
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('loading');
  let finishFonts!: () => void;
  let cancelMotion!: (reason: Error) => void;
  Object.defineProperty(document, 'getAnimations', {
    configurable: true,
    value: () => [
      {
        effect: {getComputedTiming: () => ({endTime: 350})},
        finished: new Promise((_, reject) => {
          cancelMotion = reject;
        }),
      },
      {
        effect: {getComputedTiming: () => ({endTime: Infinity})},
        finished: new Promise(() => {}),
      },
    ],
  });
  Object.defineProperty(document, 'fonts', {
    configurable: true,
    value: {
      ready: new Promise<void>(resolve => {
        finishFonts = resolve;
      }),
    },
  });
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal('requestAnimationFrame', (frame: FrameRequestCallback) =>
    frames.push(frame),
  );
  const callback = vi.fn();
  const idle = vi.fn();
  vi.stubGlobal('requestIdleCallback', idle);
  afterFirstView(callback);
  expect(frames).toHaveLength(0);
  window.dispatchEvent(new Event('load'));
  window.dispatchEvent(new Event('load'));
  expect(frames).toHaveLength(1);
  frames.shift()!(0);
  expect(callback).not.toHaveBeenCalled();
  expect(frames).toHaveLength(0);
  finishFonts();
  await Promise.resolve();
  expect(frames).toHaveLength(0);
  cancelMotion(new Error('Motion cancelled by navigation'));
  await Promise.resolve();
  await Promise.resolve();
  expect(frames).toHaveLength(1);
  frames.shift()!(16);
  expect(idle).not.toHaveBeenCalled();
  frames.shift()!(32);
  expect(callback).not.toHaveBeenCalled();
  expect(idle).toHaveBeenCalledWith(callback, {timeout: 2000});
  idle.mock.calls[0][0]();
  expect(callback).toHaveBeenCalledOnce();
});

it('schedules after an already loaded page when the font API is unavailable', async () => {
  vi.useFakeTimers();
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('complete');
  Object.defineProperty(document, 'fonts', {
    configurable: true,
    value: undefined,
  });
  const callback = vi.fn();
  afterFirstView(callback);
  await vi.runAllTimersAsync();
  expect(callback).toHaveBeenCalledOnce();
});
