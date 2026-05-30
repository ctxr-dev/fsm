import type { JSX, VNode } from 'preact';
import { useCallback, useMemo, useRef, useState } from 'preact/hooks';

export interface TreeNode {
  /** Stable id; used as React-style key and ARIA target. */
  id: string;
  /** Label rendered for the node. May be a string or a VNode. */
  label: string | VNode;
  /** Optional child nodes. */
  children?: TreeNode[];
  /** Optional icon rendered before the label. */
  icon?: VNode;
  /** Default expanded state for this node (only used on first render). */
  defaultExpanded?: boolean;
}

export interface TreeProps {
  nodes: TreeNode[];
  /** ARIA label for the tree role. */
  label?: string;
  /** Called when the user activates (Enter / Space / click) a node. */
  onActivate?: (node: TreeNode) => void;
  className?: string;
}

interface FlatNode {
  node: TreeNode;
  depth: number;
  parentId: string | null;
  hasChildren: boolean;
  expanded: boolean;
}

function flatten(
  nodes: TreeNode[],
  expanded: Set<string>,
  depth = 0,
  parentId: string | null = null,
  out: FlatNode[] = [],
): FlatNode[] {
  for (const node of nodes) {
    const hasChildren = Array.isArray(node.children) && node.children.length > 0;
    const isExpanded = expanded.has(node.id);
    out.push({ node, depth, parentId, hasChildren, expanded: isExpanded });
    if (hasChildren && isExpanded) {
      flatten(node.children!, expanded, depth + 1, node.id, out);
    }
  }
  return out;
}

function collectDefaults(
  nodes: TreeNode[],
  set: Set<string> = new Set(),
): Set<string> {
  for (const n of nodes) {
    if (n.defaultExpanded) set.add(n.id);
    if (n.children?.length) collectDefaults(n.children, set);
  }
  return set;
}

/**
 * Tree — collapsible ARIA tree.
 *
 * Keyboard model (WAI-ARIA tree pattern, single-select):
 * - ArrowDown / ArrowUp — move focus to next / previous visible node.
 * - ArrowRight — expand if collapsed, otherwise focus first child.
 * - ArrowLeft  — collapse if expanded, otherwise focus parent.
 * - Home / End — focus first / last visible node.
 * - Enter / Space — activate (calls onActivate).
 */
export function Tree({
  nodes,
  label = 'Tree',
  onActivate,
  className = '',
}: TreeProps): JSX.Element {
  const [expanded, setExpanded] = useState<Set<string>>(() =>
    collectDefaults(nodes),
  );
  const [focusedId, setFocusedId] = useState<string | null>(
    nodes.length > 0 ? nodes[0].id : null,
  );
  const containerRef = useRef<HTMLUListElement | null>(null);

  const flat = useMemo(() => flatten(nodes, expanded), [nodes, expanded]);

  const focusNode = useCallback((id: string) => {
    setFocusedId(id);
    // Defer to next tick so the DOM has rendered tabindex changes.
    queueMicrotask(() => {
      const el = containerRef.current?.querySelector<HTMLElement>(
        `[data-tree-id="${CSS.escape(id)}"]`,
      );
      el?.focus();
    });
  }, []);

  const toggle = useCallback((id: string, force?: boolean) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      const isExpanded = next.has(id);
      const shouldExpand = force ?? !isExpanded;
      if (shouldExpand) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const handleKeyDown = (item: FlatNode) => (event: KeyboardEvent) => {
    const idx = flat.findIndex((f) => f.node.id === item.node.id);
    switch (event.key) {
      case 'ArrowDown': {
        event.preventDefault();
        const next = flat[idx + 1];
        if (next) focusNode(next.node.id);
        break;
      }
      case 'ArrowUp': {
        event.preventDefault();
        const prev = flat[idx - 1];
        if (prev) focusNode(prev.node.id);
        break;
      }
      case 'ArrowRight': {
        event.preventDefault();
        if (item.hasChildren && !item.expanded) {
          toggle(item.node.id, true);
        } else if (item.hasChildren && item.expanded) {
          const child = flat[idx + 1];
          if (child) focusNode(child.node.id);
        }
        break;
      }
      case 'ArrowLeft': {
        event.preventDefault();
        if (item.hasChildren && item.expanded) {
          toggle(item.node.id, false);
        } else if (item.parentId) {
          focusNode(item.parentId);
        }
        break;
      }
      case 'Home': {
        event.preventDefault();
        if (flat[0]) focusNode(flat[0].node.id);
        break;
      }
      case 'End': {
        event.preventDefault();
        const last = flat[flat.length - 1];
        if (last) focusNode(last.node.id);
        break;
      }
      case 'Enter':
      case ' ': {
        event.preventDefault();
        if (item.hasChildren) toggle(item.node.id);
        onActivate?.(item.node);
        break;
      }
      default:
        break;
    }
  };

  const composed = [
    'text-sm text-slate-900 dark:text-slate-100',
    'select-none',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <ul
      ref={containerRef}
      role="tree"
      aria-label={label}
      class={composed}
    >
      {flat.map((item) => {
        const isFocused = focusedId === item.node.id;
        return (
          <li
            key={item.node.id}
            role="treeitem"
            aria-level={item.depth + 1}
            aria-expanded={
              item.hasChildren ? item.expanded : undefined
            }
            tabIndex={isFocused ? 0 : -1}
            data-tree-id={item.node.id}
            onKeyDown={handleKeyDown(item)}
            onClick={(e) => {
              e.stopPropagation();
              focusNode(item.node.id);
              if (item.hasChildren) toggle(item.node.id);
              onActivate?.(item.node);
            }}
            class={[
              'flex items-center gap-1.5 rounded-sm cursor-pointer',
              'px-1.5 py-1',
              'hover:bg-slate-100 dark:hover:bg-slate-700/60',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-inset',
              'focus-visible:ring-emerald-500',
            ].join(' ')}
            style={{ paddingLeft: `${0.375 + item.depth * 1.25}rem` }}
          >
            <span
              aria-hidden="true"
              class={[
                'inline-flex items-center justify-center w-4 h-4 shrink-0',
                'text-slate-400 dark:text-slate-500',
                'transition-transform',
                item.expanded ? 'rotate-90' : '',
              ].join(' ')}
            >
              {item.hasChildren ? (
                /* Right-pointing chevron — rotates 90deg when expanded. */
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  viewBox="0 0 16 16"
                  fill="currentColor"
                  class="w-3 h-3"
                >
                  <path d="M6 3.5L10.5 8 6 12.5V3.5z" />
                </svg>
              ) : (
                <span class="block w-1.5 h-1.5 rounded-full bg-slate-300 dark:bg-slate-600" />
              )}
            </span>
            {item.node.icon ? (
              <span aria-hidden="true" class="shrink-0">
                {item.node.icon}
              </span>
            ) : null}
            <span class="truncate">{item.node.label}</span>
          </li>
        );
      })}
    </ul>
  );
}

export default Tree;
