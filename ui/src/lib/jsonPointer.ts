/**
 * RFC 6901 JSON Pointer helpers.
 *
 * Used by the W18b `<JsonViewer />` to emit semantic identifiers when a
 * user clicks a label (`/items/0/price`) and to drive the breadcrumb
 * trail at the top of the viewer.
 *
 * Encoding rules per RFC 6901 §3:
 *
 *   '~' → '~0'   (escape the escape char FIRST)
 *   '/' → '~1'   (escape the path separator)
 *
 * The order matters: if we substituted '/' first, an actual '~' in a
 * key would later collide with our just-written '~1' escape sequence
 * during decoding. Encoding '~' first ensures the two substitutions
 * are independent.
 *
 * Pointer shape: a string starting with '/' followed by zero or more
 * '/'-separated escaped segments. The empty string is the root
 * pointer per RFC 6901; we expose it as `ROOT_POINTER` for clarity.
 */

export type JsonPointer = string;

/** Per RFC 6901 §5: the root document is addressed by the empty string. */
export const ROOT_POINTER: JsonPointer = '';

/**
 * Encode a single path segment (object key or array index).
 *
 *   escapeSegment('foo')      === 'foo'
 *   escapeSegment('a/b')      === 'a~1b'
 *   escapeSegment('~tilde')   === '~0tilde'
 *   escapeSegment('a/b~c')    === 'a~1b~0c'      // ~ first, then /
 *   escapeSegment(0)          === '0'
 */
export function escapeSegment(segment: string | number): string {
  const s = String(segment);
  return s.replace(/~/g, '~0').replace(/\//g, '~1');
}

/**
 * Decode a single segment back to its original key.
 *
 * Inverse of escapeSegment. Order is the reverse:
 *
 *   '/' decode FIRST (so we don't accidentally restore '~0' produced by
 *   a previous '~' that should have stayed literal), then '~'.
 *
 *   unescapeSegment('a~1b')   === 'a/b'
 *   unescapeSegment('~0tilde')=== '~tilde'
 *   unescapeSegment('a~1b~0c')=== 'a/b~c'
 *
 * Invalid sequences (`~` followed by anything other than `0` or `1`)
 * are returned verbatim; the spec leaves their behaviour
 * implementation-defined, and we choose forgiving-passthrough so a
 * mis-encoded pointer from a misbehaving consumer doesn't crash the
 * viewer.
 */
export function unescapeSegment(segment: string): string {
  return segment.replace(/~1/g, '/').replace(/~0/g, '~');
}

/**
 * Append a segment to a parent pointer.
 *
 *   joinPointer('', 'a')         === '/a'
 *   joinPointer('/a', 'b')       === '/a/b'
 *   joinPointer('/items', 0)     === '/items/0'
 *   joinPointer('', 'a/b')       === '/a~1b'   // escapes the slash
 */
export function joinPointer(
  parent: JsonPointer,
  segment: string | number,
): JsonPointer {
  return `${parent}/${escapeSegment(segment)}`;
}

/**
 * Build a pointer from a sequence of path segments. The root document
 * is `[]` → `''`. Useful for the JsonViewer's flatten step which knows
 * a node's depth as an array of ancestor keys.
 *
 *   pointerForPath([])           === ''
 *   pointerForPath(['a', 'b'])   === '/a/b'
 *   pointerForPath(['a/b', 0])   === '/a~1b/0'
 */
export function pointerForPath(segments: readonly (string | number)[]): JsonPointer {
  let out: JsonPointer = ROOT_POINTER;
  for (const seg of segments) {
    out = joinPointer(out, seg);
  }
  return out;
}

/**
 * Split a pointer back into its decoded segments. Inverse of
 * pointerForPath. Returns `[]` for the root pointer; throws TypeError
 * for a non-pointer (anything that doesn't start with '/' and isn't
 * empty), to fail loudly when a consumer hands us a string they
 * thought was a JSON Pointer but isn't.
 *
 *   parsePointer('')           === []
 *   parsePointer('/a')         === ['a']
 *   parsePointer('/a/b')       === ['a', 'b']
 *   parsePointer('/a~1b/0')    === ['a/b', '0']     // ARRAY INDICES stay strings;
 *                                                   // callers decide
 *   parsePointer('foo')        throws TypeError     // missing leading '/'
 */
export function parsePointer(pointer: JsonPointer): string[] {
  if (pointer === ROOT_POINTER) return [];
  if (!pointer.startsWith('/')) {
    throw new TypeError(
      `Invalid JSON Pointer (must be empty or start with "/"): ${JSON.stringify(pointer)}`,
    );
  }
  return pointer
    .slice(1)
    .split('/')
    .map(unescapeSegment);
}
