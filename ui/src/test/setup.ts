import '@testing-library/jest-dom';

// jsdom under node 25 emits a `--localstorage-file` warning + ships
// localStorage with undefined setItem / getItem methods. Shim a real
// in-memory Storage implementation on the window so code that uses
// `window.localStorage` (the W18a persistence layer in `lib/store.ts`,
// every settings panel, the command palette's recent-queries history)
// hits a working API in tests.

class InMemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length(): number {
    return this.map.size;
  }
  key(index: number): string | null {
    const keys = Array.from(this.map.keys());
    return keys[index] ?? null;
  }
  getItem(key: string): string | null {
    return this.map.has(key) ? this.map.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.map.set(String(key), String(value));
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  clear(): void {
    this.map.clear();
  }
}

Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: new InMemoryStorage(),
});
Object.defineProperty(window, 'sessionStorage', {
  configurable: true,
  value: new InMemoryStorage(),
});
