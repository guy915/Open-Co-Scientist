// Browser page translation (Chrome, Google Translate) swaps React's text nodes
// for its own <font> wrappers. React later removes or inserts beside a node
// that is no longer where it left it, the DOM throws NotFoundError, and the
// page crashes into the error screen. On a translated page only, skip that
// one stale DOM step instead of crashing; untranslated pages keep the native
// errors so real bugs still surface.
function pageIsTranslated(): boolean {
  return /\btranslated-(ltr|rtl)\b/.test(document.documentElement.className);
}

let installed = false;

export function installTranslateGuard(): void {
  if (installed || typeof Node === 'undefined') return;
  installed = true;
  const removeChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function <T extends Node>(
    this: Node,
    child: T,
  ): T {
    if (child.parentNode !== this && pageIsTranslated()) return child;
    return removeChild.call(this, child) as T;
  };
  const insertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function <T extends Node>(
    this: Node,
    node: T,
    child: Node | null,
  ): T {
    if (child && child.parentNode !== this && pageIsTranslated()) return node;
    return insertBefore.call(this, node, child) as T;
  };
}
