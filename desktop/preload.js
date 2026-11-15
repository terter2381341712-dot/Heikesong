'use strict';

// Only the events and the three recovery actions the status page needs cross
// the context boundary; the renderer never touches Node or the filesystem.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('edgeFabric', {
  onLog: (handler) => ipcRenderer.on('log', (_event, line) => handler(line)),
  onReady: (handler) => ipcRenderer.on('ready', (_event, url) => handler(url)),
  onFailed: (handler) => ipcRenderer.on('failed', (_event, message) => handler(message)),
  onStopping: (handler) => ipcRenderer.on('stopping', (_event, message) => handler(message)),
  retry: () => ipcRenderer.send('retry'),
  openLog: () => ipcRenderer.send('open-log'),
  logPath: () => ipcRenderer.invoke('log-path'),
});
