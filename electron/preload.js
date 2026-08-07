/**
 * The entire Electron→renderer surface.
 *
 * Deliberately tiny: the renderer is the same code a browser loads, so the only
 * things exposed are the two capabilities a browser genuinely can't provide —
 * a native folder dialog and native menu accelerators.
 */

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agentMonitor', {
  /** Native OS directory picker. Resolves to an absolute path, or null. */
  pickDirectory: () => ipcRenderer.invoke('agentmon:pickDirectory'),

  /** Menu items and accelerators — 'open' | 'refresh' | 'fit' | 'settings'. */
  onCommand: (handler) => {
    ipcRenderer.on('agentmon:command', (_event, command, payload) => handler(command, payload));
  },

  platform: process.platform,
});
