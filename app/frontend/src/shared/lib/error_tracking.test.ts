import type {BrowserOptions, ErrorEvent} from '@sentry/browser';
import {afterEach, describe, expect, it, vi} from 'vitest';
const init = vi.hoisted(() => vi.fn());
vi.mock('@sentry/browser', () => ({init}));
import {initErrorTracking, privateErrorEvent} from './error_tracking';

interface Options {
  dataCollection: BrowserOptions['dataCollection'];
  maxBreadcrumbs: number;
  tracesSampleRate: number;
  beforeBreadcrumb: (breadcrumb: unknown) => unknown;
  beforeSend: (event: ErrorEvent, hint: {attachments: unknown[]}) => unknown;
}

describe('private error tracking', () => {
  afterEach(() => {
    init.mockReset();
    document
      .querySelectorAll('script[data-fixture]')
      .forEach(node => node.remove());
  });

  it('drops unknown private strings throughout an event without mutating it', () => {
    const secret =
      'PRIVATE_GOAL PRIVATE_DOCUMENT scientist@example.test sk-unknown';
    const event: ErrorEvent = {
      type: undefined,
      message: secret,
      logentry: {message: secret, params: [secret]},
      user: {email: 'scientist@example.test'},
      request: {url: `https://example.test/?goal=${secret}`, data: secret},
      breadcrumbs: [{message: secret, data: {secret}}],
      contexts: {custom: {secret}},
      extra: {secret},
      tags: {secret},
      fingerprint: [secret],
      transaction: secret,
      release: secret,
      environment: secret,
      exception: {
        values: [
          {
            type: secret,
            value: secret,
            stacktrace: {
              frames: [
                {
                  filename: `https://example.test/${secret}`,
                  function: secret,
                  context_line: secret,
                  vars: {secret},
                  lineno: 42,
                },
              ],
            },
          },
        ],
      },
    };
    const projected = privateErrorEvent(event);
    for (const marker of secret.split(' '))
      expect(JSON.stringify(projected)).not.toContain(marker);
    expect(projected.exception?.values?.[0].type).toBe('Error');
    expect(
      projected.exception?.values?.[0].stacktrace?.frames?.[0].lineno,
    ).toBe(42);
    expect(event.message).toBe(secret);
  });

  it('keeps native error classification and trusted bundle position without query text', () => {
    const script = document.createElement('script');
    script.dataset.fixture = 'true';
    script.src = `${location.origin}/assets/index-fixture.js`;
    document.head.appendChild(script);
    const projected = privateErrorEvent({
      type: undefined,
      exception: {
        values: [
          {
            type: 'TypeError',
            value: 'PRIVATE_DOCUMENT',
            stacktrace: {
              frames: [
                {
                  filename: `${script.src}?email=scientist@example.test`,
                  lineno: 12,
                  colno: 8,
                  function: 'PRIVATE_GOAL',
                },
              ],
            },
          },
        ],
      },
    });
    expect(projected.exception?.values?.[0]).toMatchObject({
      type: 'TypeError',
      stacktrace: {
        frames: [
          {
            filename: '/assets/index-fixture.js',
            lineno: 12,
            colno: 8,
          },
        ],
      },
    });
    expect(JSON.stringify(projected)).not.toContain('PRIVATE_');
    expect(JSON.stringify(projected)).not.toContain('scientist@example.test');
  });

  it('disables breadcrumbs and traces and removes out-of-band attachments', () => {
    initErrorTracking('https://key@sentry.example/1');
    const options = init.mock.calls[0][0] as Options;
    expect(options.dataCollection).toEqual({
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      httpBodies: [],
      urlQueryParams: false,
      graphQL: {document: false, variables: false},
      genAI: {inputs: false, outputs: false},
      databaseQueryData: false,
      queues: false,
      stackFrameVariables: false,
      frameContextLines: 0,
    });
    expect(options.maxBreadcrumbs).toBe(0);
    expect(options.tracesSampleRate).toBe(0);
    for (const category of [
      'console',
      'navigation',
      'fetch',
      'xhr',
      'ui.click',
    ]) {
      expect(
        options.beforeBreadcrumb({category, message: 'PRIVATE_GOAL'}),
      ).toBeNull();
    }
    const hint = {attachments: [{data: 'PRIVATE_DOCUMENT'}]};
    expect(
      options.beforeSend({type: undefined, message: 'PRIVATE_GOAL'}, hint),
    ).toMatchObject({
      message: 'Application error',
    });
    expect(hint.attachments).toEqual([]);
  });

  it('sends a private envelope through the real SDK transport', async () => {
    const sdk =
      await vi.importActual<typeof import('@sentry/browser')>(
        '@sentry/browser',
      );
    initErrorTracking('https://key@sentry.example/1');
    const envelopes: unknown[] = [];
    const options = init.mock.calls[0][0] as Parameters<typeof sdk.init>[0];
    const client = new sdk.BrowserClient({
      ...options,
      dsn: 'https://key@sentry.example/1',
      stackParser: sdk.defaultStackParser,
      integrations: [],
      transport: () => ({
        send: async envelope => {
          envelopes.push(envelope);
          return {statusCode: 200};
        },
        flush: async () => true,
      }),
    });
    client.captureEvent(
      {
        message: 'PRIVATE_GOAL scientist@example.test',
        extra: {document: 'PRIVATE_DOCUMENT'},
        user: {email: 'scientist@example.test'},
        exception: {values: [{type: 'TypeError', value: 'sk-unknown-key'}]},
      },
      {attachments: [{filename: 'private.txt', data: 'PRIVATE_DOCUMENT'}]},
    );
    const session = sdk.startSession({release: 'privacy-fixture'});
    client.captureSession(session);
    sdk.endSession();
    expect(await client.flush(1000)).toBe(true);
    expect(envelopes).toHaveLength(2);
    const sent = JSON.stringify(envelopes);
    for (const marker of [
      'PRIVATE_GOAL',
      'PRIVATE_DOCUMENT',
      'scientist@example.test',
      'sk-unknown-key',
    ])
      expect(sent).not.toContain(marker);
    expect(sent).toContain('TypeError');
    expect(sent).toContain('Application error');
    expect(sent).toContain('"infer_ip":"never"');
    expect(sent).not.toContain('"infer_ip":"auto"');
    expect(sent).not.toContain('ip_address');
    await client.close();
  });
});
