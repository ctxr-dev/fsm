import { render } from 'preact';
import { App } from './app';
import { loadStoredPrefs, wirePrefsPersistence, wireProjectMetadata } from './lib/store';

// Hydrate persisted prefs (theme, density, urlStateEnabled, recents)
// BEFORE first render so the initial paint reflects the user's last
// choice. Then wire the debounced effect that writes future changes
// back to localStorage.
loadStoredPrefs();
wirePrefsPersistence();
// W23c: fetch + memoise project metadata once at boot so every route
// reads from the same signal instead of issuing redundant
// /projects/current calls. Refreshes on window focus.
wireProjectMetadata();

const root = document.getElementById('app');
if (!root) {
  throw new Error('Root element #app not found');
}
render(<App />, root);
