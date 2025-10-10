import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'path';
import { fileURLToPath } from 'url';

interface ServerHandle {
  port: number;
  close: () => Promise<void>;
}

type StartServerFn = (port?: number) => Promise<ServerHandle>;

function resolveStartServer(): StartServerFn | null {
  try {
    const modulePath = path.resolve(__dirname, '..', 'dist', 'server', 'index.js');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require(modulePath);
    return mod.startServer as StartServerFn;
  } catch (error) {
    try {
      const modulePath = path.resolve(__dirname, '..', 'build', 'server', 'index.js');
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const mod = require(modulePath);
      return mod.startServer as StartServerFn;
    } catch {
      return null;
    }
  }
}

let serverHandle: ServerHandle | null = null;
const startServer = resolveStartServer();

async function ensureServer(): Promise<ServerHandle | null> {
  if (serverHandle) {
    return serverHandle;
  }
  if (!startServer) {
    return null;
  }
  const port = Number(process.env.PORT ?? 7788);
  serverHandle = await startServer(port);
  return serverHandle;
}

async function createWindow() {
  await ensureServer();
  const isDev = process.env.NODE_ENV === 'development';
  const preloadPath = path.join(__dirname, 'preload.js');
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    backgroundColor: '#0d1117',
    titleBarStyle: 'hiddenInset',
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      devTools: true
    }
  });

  const devUrl = process.env.VITE_DEV_SERVER_URL ?? 'http://localhost:5173';
  if (isDev) {
    await win.loadURL(devUrl);
    win.webContents.openDevTools({ mode: 'detach' });
  } else {
    const indexHtml = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'index.html');
    await win.loadFile(indexHtml);
  }
}

app.on('window-all-closed', async () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
  if (serverHandle) {
    await serverHandle.close().catch(() => undefined);
    serverHandle = null;
  }
});

app.whenReady().then(async () => {
  ipcMain.handle('server-info', async () => {
    const handle = await ensureServer();
    return { port: handle?.port ?? Number(process.env.PORT ?? 7788) };
  });
  await createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      void createWindow();
    }
  });
});
