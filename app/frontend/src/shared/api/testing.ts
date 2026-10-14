import {vi} from 'vitest';

export function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

export function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {'Content-Type': 'application/json'},
  });
}

export function errorResponse(status: number, text = 'boom'): Response {
  return new Response(text, {status, statusText: 'Error'});
}

export function unreadableErrorResponse(
  status: number,
  statusText: string,
): Response {
  const body = new ReadableStream<Uint8Array>({
    start: controller => controller.error(new Error('stream closed')),
  });
  return new Response(body, {status, statusText});
}

export class FakeSseBody {
  private controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  readonly stream = new ReadableStream<Uint8Array>({
    start: controller => {
      this.controller = controller;
    },
  });
  private readonly encoder = new TextEncoder();

  push(frame: unknown): void {
    this.pushRaw(JSON.stringify(frame));
  }

  pushRaw(data: string): void {
    this.controller?.enqueue(this.encoder.encode(`data: ${data}\n\n`));
  }

  end(): void {
    this.controller?.close();
  }
}

export function streamingResponse(body: FakeSseBody): Response {
  return new Response(body.stream, {
    status: 200,
    headers: {'Content-Type': 'text/event-stream'},
  });
}
