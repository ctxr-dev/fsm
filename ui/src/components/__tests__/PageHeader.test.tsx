/**
 * Tests for components/PageHeader.tsx.
 *
 * Covers the three eyebrow modes:
 *   1. metadata + resolvable name -> bold project name.
 *   2. loading -> italic "loading…".
 *   3. metadata present but name resolves to null (older API / null
 *      project_root + null project_slug / Windows drive root) ->
 *      explicit italic "no project bound" affordance.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/preact';

import { PageHeader } from '../PageHeader';
import { projectMetadata, projectMetadataLoading } from '../../lib/store';

describe('PageHeader eyebrow', () => {
  beforeEach(() => {
    projectMetadata.value = null;
    projectMetadataLoading.value = false;
  });

  it('renders the project name when metadata + project_root resolve', () => {
    projectMetadata.value = {
      project_root: '/Users/x/dummy-fsm-test',
      project_slug: 'dummy-fsm-test',
    } as never;
    const { getByText } = render(<PageHeader title="Runs" />);
    expect(getByText('dummy-fsm-test')).toBeTruthy();
  });

  it('falls back to the slug when project_root is null', () => {
    projectMetadata.value = {
      project_root: null,
      project_slug: 'my-slug',
    } as never;
    const { getByText } = render(<PageHeader title="Runs" />);
    expect(getByText('my-slug')).toBeTruthy();
  });

  it('renders "loading…" while the metadata fetch is in flight', () => {
    projectMetadataLoading.value = true;
    const { getByText } = render(<PageHeader title="Runs" />);
    expect(getByText('loading…')).toBeTruthy();
  });

  it('renders "no project bound" when metadata is present but name is null', () => {
    // Older in-memory backend: project_root + project_slug both null.
    projectMetadata.value = {
      project_root: null,
      project_slug: null,
    } as never;
    const { getByText } = render(<PageHeader title="Runs" />);
    expect(getByText('no project bound')).toBeTruthy();
  });

  it('renders "no project bound" for a Windows drive-root project_root', () => {
    projectMetadata.value = {
      project_root: 'C:\\',
      project_slug: null,
    } as never;
    const { getByText } = render(<PageHeader title="Runs" />);
    expect(getByText('no project bound')).toBeTruthy();
  });

  it('does NOT render the eyebrow when projectAware is false', () => {
    projectMetadata.value = {
      project_root: '/Users/x/proj',
      project_slug: 'proj',
    } as never;
    const { queryByText } = render(
      <PageHeader title="Runs" projectAware={false} />,
    );
    expect(queryByText('proj')).toBeNull();
  });
});
