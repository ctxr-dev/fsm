import { render } from 'preact';
import { App } from './app';
import { loadStoredPrefs, wirePrefsPersistence } from './lib/store';

// Hydrate persisted prefs (theme, density, urlStateEnabled, recents)
// BEFORE first render so the initial paint reflects the user's last
// choice. Then wire the debounced effect that writes future changes
// back to localStorage.
loadStoredPrefs();
wirePrefsPersistence();

const root = document.getElementById('app');
if (!root) {
  throw new Error('Root element #app not found');
}
render(<App />, root);
