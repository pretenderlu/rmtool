import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    root: path.resolve(__dirname, 'src/web'),
    plugins: [react()],
    server: {
      port: Number(env.VITE_PORT ?? 5173),
      proxy: {
        '/api': {
          target: env.VITE_API_URL ?? 'http://localhost:7788',
          changeOrigin: true
        },
        '/thumbnails': {
          target: env.VITE_API_URL ?? 'http://localhost:7788',
          changeOrigin: true
        }
      }
    },
    build: {
      outDir: path.resolve(__dirname, 'dist'),
      emptyOutDir: true
    }
  };
});
