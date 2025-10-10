import express from 'express';
import cors from 'cors';
import path from 'path';
import bodyParser from 'body-parser';
import deviceRoutes from './routes/deviceRoutes.js';

const app = express();

app.use(cors());
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));
app.get('/health', (_req, res) => res.json({ ok: true }));
app.use('/api/devices', deviceRoutes);
app.use('/thumbnails', express.static(path.resolve('cache', 'thumbnails')));

export interface ServerHandle {
  port: number;
  close: () => Promise<void>;
}

export function startServer(port: number = Number(process.env.PORT ?? 7788)): Promise<ServerHandle> {
  return new Promise((resolve, reject) => {
    const server = app
      .listen(port, () => {
        resolve({
          port,
          close: () =>
            new Promise<void>((closeResolve, closeReject) => {
              server.close((error) => {
                if (error) {
                  closeReject(error);
                } else {
                  closeResolve();
                }
              });
            })
        });
      })
      .on('error', (error) => {
        reject(error);
      });
  });
}

