import { contextBridge, ipcRenderer } from 'electron';

declare global {
  interface Window {
    rmtool: {
      getServerInfo: () => Promise<{ port: number }>;
    };
  }
}

contextBridge.exposeInMainWorld('rmtool', {
  getServerInfo: () => ipcRenderer.invoke('server-info')
});
