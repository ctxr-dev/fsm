import { defineConfig } from 'vitest/config';
import preact from '@preact/preset-vite';
import tailwindcss from '@tailwindcss/vite';

const apiPort = process.env.VITE_API_PORT ?? '8765';

export default defineConfig({
  plugins: [preact(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api/v1': {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    // jsdom defaults to about:blank, which produces an "opaque origin"
    // and rejects localStorage / sessionStorage access with SecurityError.
    // Pin a real URL so the store-persistence tests + urlState tests
    // exercise the real browser surface.
    environmentOptions: {
      jsdom: {
        url: 'http://localhost/',
      },
    },
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
});
