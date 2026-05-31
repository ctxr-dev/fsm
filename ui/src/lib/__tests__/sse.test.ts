import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventStream } from '../sse';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, ((e: MessageEvent<string>) => void)[]>();
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, cb: (e: MessageEvent<string>) => void) {
    const list = this.listeners.get(name) ?? [];
    list.push(cb);
    this.listeners.set(name, list);
  }
  close() { this.closed = true; }
  emit(name: string, data: string) {
    for (const cb of this.listeners.get(name) ?? []) {
      cb({ data } as MessageEvent<string>);
    }
  }
}

describe('EventStream', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('reconnects with exponential backoff and ignores ping frames', () => {
    const received: unknown[] = [];
    const stream = new EventStream(
      '/api/v1/events/stream',
      { consumer_name: 'test' },
      FakeEventSource as unknown as typeof EventSource,
    );
    stream.on((e) => received.push(e));
    expect(FakeEventSource.instances).toHaveLength(1);

    // ping is filtered out
    FakeEventSource.instances[0].emit('ping', '');
    expect(received).toHaveLength(0);

    // trigger error, advance backoff (1s), confirm new instance
    FakeEventSource.instances[0].onerror?.();
    expect(stream.connectionState.value).toBe('error');
    vi.advanceTimersByTime(1_000);
    expect(FakeEventSource.instances).toHaveLength(2);

    stream.close();
    expect(stream.connectionState.value).toBe('closed');
  });
});
