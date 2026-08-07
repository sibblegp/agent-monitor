/**
 * The scripted "agent working" loop the hero plays: pure data, no drawing.
 *
 * Each beat mirrors a real product state — modified (amber, single pulse),
 * added (emerald, blink), signature change (pink, double pulse), a new class
 * assembling, and a removal that leaves a crimson ghost.
 */

export const LOOP_MS = 36000;

export const SCENARIO = [
  { at: 3500,  type: 'modify', id: 'rescan',
    caption: 'modified  rescan()  src/engine.py  +14 −3' },

  { at: 7500,  type: 'add', id: 'invalidate', kind: 'function', parent: 'engine', label: 'invalidate()',
    caption: 'added  invalidate()  src/engine.py  +21' },
  { at: 8100,  type: 'flow', from: 'rescan', to: 'invalidate' },

  { at: 12500, type: 'signature', id: 'parse',
    caption: 'signature changed  parse(source, lang)  src/parser.py' },
  { at: 12900, type: 'flow', from: 'tokenize', to: 'parse' },
  { at: 13100, type: 'flow', from: 'rescan', to: 'parse' },

  { at: 18000, type: 'add', id: 'Cache', kind: 'class', parent: 'model', label: 'Cache',
    caption: 'added  class Cache  src/model.py  +48' },
  { at: 19200, type: 'add', id: 'Cache.get', kind: 'method', parent: 'Cache', label: 'get()' },
  { at: 20200, type: 'add', id: 'Cache.put', kind: 'method', parent: 'Cache', label: 'put()',
    caption: 'added  Cache.get() · Cache.put()  src/model.py' },
  { at: 20800, type: 'flow', from: 'invalidate', to: 'Cache.put' },

  { at: 25500, type: 'remove', id: 'legacy',
    caption: 'removed  to_json()  src/model.py  −32' },

  { at: 29500, type: 'caption',
    caption: 'agent idle — 6 symbols changed across 3 files' },

  { at: 33500, type: 'fade' },
];
