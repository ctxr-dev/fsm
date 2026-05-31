/**
 * Tests for lib/clipboard.ts: copyText + copyTextWithResult.
 *
 * Both paths exercised:
 *   - Modern: navigator.clipboard.writeText (mocked + made-to-throw).
 *   - Legacy: document.execCommand('copy') (jsdom honours it).
 */

import { describe, expect, test, vi, afterEach } from 'vitest';

import { copyText, copyTextWithResult } from '../clipboard';

afterEach(() => {
  vi.unstubAllGlobals();
  // Restore document.execCommand to a known default in case a test mutated it.
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    writable: true,
    value: vi.fn().mockReturnValue(true),
  });
});

describe('copyText', () => {
  test('uses navigator.clipboard.writeText when available + returns true', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const ok = await copyText('hello');
    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith('hello');
  });

  test('falls back to execCommand when clipboard API throws', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('not focused'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const exec = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: exec,
    });
    const ok = await copyText('fallback-text');
    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalled();
    expect(exec).toHaveBeenCalledWith('copy');
  });

  test('falls back to execCommand when clipboard API is absent', async () => {
    vi.stubGlobal('navigator', {});
    const exec = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: exec,
    });
    const ok = await copyText('no-api');
    expect(ok).toBe(true);
    expect(exec).toHaveBeenCalled();
  });

  test('returns false when both paths fail', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: vi.fn().mockReturnValue(false),
    });
    const ok = await copyText('nope');
    expect(ok).toBe(false);
  });

  test('execCommand path cleans up the temporary textarea on success', async () => {
    vi.stubGlobal('navigator', {});
    const exec = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: exec,
    });
    const before = document.body.children.length;
    await copyText('cleanup-me');
    expect(document.body.children.length).toBe(before);
  });

  test('execCommand path cleans up the temporary textarea on failure', async () => {
    vi.stubGlobal('navigator', {});
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      writable: true,
      value: vi.fn().mockImplementation(() => {
        throw new Error('boom');
      }),
    });
    const before = document.body.children.length;
    const ok = await copyText('still-clean');
    expect(ok).toBe(false);
    expect(document.body.children.length).toBe(before);
  });
});

describe('copyTextWithResult', () => {
  test('returns text + ok tuple', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const r = await copyTextWithResult('payload');
    expect(r).toEqual({ text: 'payload', ok: true });
  });
});
