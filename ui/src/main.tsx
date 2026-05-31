import { render } from 'preact';
import { App } from './app';

const root = document.getElementById('app');
if (!root) {
  throw new Error('Root element #app not found');
}
render(<App />, root);
