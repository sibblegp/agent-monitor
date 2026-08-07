/**
 * Agent Monitor native shell.
 *
 * All the real work is Python. This process spawns the backend, learns its port
 * from a one-line JSON handshake on stdout, and points a window at it. The
 * renderer is the same code a plain browser loads — the only thing Electron
 * adds is a real window, a menu, and the native folder picker.
 */

'use strict';

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');

// Render natively on Wayland (Hyprland) instead of going through XWayland.
// Must be set before `whenReady`.
app.commandLine.appendSwitch('ozone-platform-hint', 'auto');
app.setName('Agent Monitor');

let backend = null;
let win = null;
let ready = null; // { port, token, url }
let quitting = false;

/**
 * The repository to open, from `--repo <path>` or `--repo=<path>`.
 *
 * Deliberately not positional: Electron's argv includes the app directory, so a
 * positional argument is ambiguous with it.
 */
function repoArg() {
  const argv = process.argv;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--repo' && argv[i + 1]) return argv[i + 1];
    if (argv[i].startsWith('--repo=')) return argv[i].slice('--repo='.length);
  }
  return null;
}

/** Prefer the project's virtualenv, then a `agent_monitor` on PATH, then bare python. */
function pythonCandidates() {
  const isWin = process.platform === 'win32';
  const venv = isWin
    ? path.join(ROOT, 'env_cv', 'Scripts', 'python.exe')
    : path.join(ROOT, 'env_cv', 'bin', 'python');
  const list = [];
  if (fs.existsSync(venv)) list.push(venv);
  list.push(isWin ? 'python.exe' : 'python3', 'python');
  return list;
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const [exe, ...fallbacks] = pythonCandidates();
    const args = ['-m', 'agent_monitor', '--port', '0', '--no-browser'];

    // Open whatever the user passed on the command line, if anything.
    //
    // Read from an explicit --repo flag rather than a positional argument.
    // Electron's argv also contains the app directory, and a bare positional
    // was picking *that* up as the repo: launching from the project root
    // silently opened `electron/` and watched only that subtree.
    const target = repoArg();
    if (target) args.push(target);

    const child = spawn(exe, args, {
      cwd: ROOT,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    let settled = false;
    let buffer = '';

    const fail = (err) => {
      if (settled) return;
      settled = true;
      reject(err);
    };

    child.on('error', (err) => {
      if (fallbacks.length) {
        // Try the next interpreter rather than dying on a missing venv.
        child.removeAllListeners();
        startBackendWith(fallbacks, args).then(resolve, fail);
      } else {
        fail(err);
      }
    });

    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      let index;
      while ((index = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line.startsWith('{')) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.event === 'ready' && !settled) {
            settled = true;
            resolve({ child, ...msg });
          }
        } catch {
          /* not the handshake line */
        }
      }
    });

    // Surface Python errors instead of showing a blank window.
    child.stderr.on('data', (chunk) => process.stderr.write(`[agent-monitor] ${chunk}`));

    child.on('exit', (code) => {
      if (!settled) fail(new Error(`backend exited with code ${code}`));
      else if (!quitting) {
        dialog.showErrorBox('Agent Monitor', `The analysis backend stopped (exit ${code}).`);
        app.quit();
      }
    });

    setTimeout(() => fail(new Error('backend did not report ready within 30s')), 30000);
  });
}

function startBackendWith(candidates, args) {
  const [exe, ...rest] = candidates;
  return new Promise((resolve, reject) => {
    const child = spawn(exe, args, { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
    let buffer = '';
    let settled = false;
    child.on('error', () =>
      rest.length ? startBackendWith(rest, args).then(resolve, reject) : reject(new Error(`cannot start ${exe}`))
    );
    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const nl = buffer.indexOf('\n');
      if (nl === -1) return;
      try {
        const msg = JSON.parse(buffer.slice(0, nl));
        if (msg.event === 'ready' && !settled) {
          settled = true;
          resolve({ child, ...msg });
        }
      } catch {
        /* keep waiting */
      }
    });
    child.stderr.on('data', (chunk) => process.stderr.write(`[agent-monitor] ${chunk}`));
  });
}

function send(command, payload) {
  win?.webContents.send('agentmon:command', command, payload ?? null);
}

async function pickDirectory() {
  const result = await dialog.showOpenDialog(win, {
    title: 'Open repository',
    properties: ['openDirectory', 'createDirectory'],
    buttonLabel: 'Open',
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
}

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'Open Repository…',
          accelerator: 'CmdOrCtrl+O',
          click: () => send('open'),
        },
        {
          label: 'Refresh',
          accelerator: 'CmdOrCtrl+R',
          click: () => send('refresh'),
        },
        { type: 'separator' },
        {
          label: 'Settings…',
          accelerator: 'CmdOrCtrl+,',
          click: () => send('settings'),
        },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Fit to Window', accelerator: 'CmdOrCtrl+0', click: () => send('fit') },
        {
          // Ctrl+R is bound to a data refresh, so reloading the UI itself
          // (to pick up frontend changes) needs its own key.
          label: 'Reload UI',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => win?.reload(),
        },
        { type: 'separator' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { role: 'resetZoom' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        { role: 'toggleDevTools' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'Open in Browser',
          click: () => ready && shell.openExternal(ready.url),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  win = new BrowserWindow({
    width: 1600,
    height: 950,
    minWidth: 900,
    minHeight: 560,
    backgroundColor: '#0a0c10',
    title: 'Agent Monitor',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    win = null;
  });

  // Keep navigation inside the app; anything else opens in the real browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Belt and braces: if the very first connection still loses a race with the
  // backend's accept loop, retry a few times before giving up.
  let attempts = 0;
  win.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || quitting || attempts >= 10) return;
    attempts += 1;
    setTimeout(() => win && !win.isDestroyed() && win.loadURL(ready.url), 250);
  });

  win.loadURL(ready.url);
}

app.whenReady().then(async () => {
  ipcMain.handle('agentmon:pickDirectory', pickDirectory);

  try {
    const started = await startBackend();
    backend = started.child;
    ready = { port: started.port, token: started.token, url: started.url };
  } catch (err) {
    dialog.showErrorBox(
      'Agent Monitor — backend failed to start',
      `${err.message}\n\n` +
        `Tried: ${pythonCandidates().join(', ')}\n\n` +
        `Install the dependencies first:\n  pip install -e .`
    );
    app.quit();
    return;
  }

  buildMenu();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

function stopBackend() {
  if (!backend) return;
  quitting = true;
  // SIGTERM lets uvicorn shut the watcher down cleanly.
  try {
    backend.kill('SIGTERM');
  } catch {
    /* already gone */
  }
  backend = null;
}

app.on('window-all-closed', () => {
  stopBackend();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', stopBackend);
process.on('exit', stopBackend);
