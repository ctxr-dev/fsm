/**
 * Tests for lib/projectName.ts: projectNameFromRoot.
 *
 * The contract: take a project_root path (POSIX or Windows, possibly
 * with trailing slashes) and return the last meaningful segment, or
 * null when the input is empty / nullish / root-only.
 */

import { describe, expect, test } from 'vitest';

import { projectNameFromRoot } from '../projectName';

describe('projectNameFromRoot', () => {
  test('returns null for nullish inputs', () => {
    expect(projectNameFromRoot(null)).toBeNull();
    expect(projectNameFromRoot(undefined)).toBeNull();
    expect(projectNameFromRoot('')).toBeNull();
  });

  test('returns last segment of a POSIX path', () => {
    expect(projectNameFromRoot('/Users/x/dummy-fsm-test')).toBe('dummy-fsm-test');
    expect(projectNameFromRoot('/Users/x/dummy-fsm-test/')).toBe('dummy-fsm-test');
    expect(projectNameFromRoot('/Users/x/dummy-fsm-test///')).toBe('dummy-fsm-test');
  });

  test('returns last segment of a Windows path', () => {
    expect(projectNameFromRoot('C:\\Users\\x\\proj')).toBe('proj');
    expect(projectNameFromRoot('C:\\Users\\x\\proj\\')).toBe('proj');
  });

  test('returns null for root-only POSIX inputs', () => {
    expect(projectNameFromRoot('/')).toBeNull();
    expect(projectNameFromRoot('//')).toBeNull();
  });

  test('returns null for Windows drive-root inputs', () => {
    // Documented contract: a drive root is not a project, the consumer
    // should render the "no project bound" affordance, not "C:".
    expect(projectNameFromRoot('C:\\')).toBeNull();
    expect(projectNameFromRoot('C:\\\\')).toBeNull();
    expect(projectNameFromRoot('D:\\')).toBeNull();
    expect(projectNameFromRoot('c:\\')).toBeNull();
  });

  test('handles relative paths sensibly', () => {
    expect(projectNameFromRoot('proj')).toBe('proj');
    expect(projectNameFromRoot('a/b/c')).toBe('c');
  });
});
