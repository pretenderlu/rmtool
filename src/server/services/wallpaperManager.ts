import fs from 'fs';
import path from 'path';
import os from 'os';
import sharp from 'sharp';
import { DeviceWithSecret } from '../config/deviceStore.js';
import { withSftp } from './sshClient.js';

export type WallpaperMode = 'fill' | 'fit';

export interface WallpaperOptions {
  mode: WallpaperMode;
  focusX?: number;
  focusY?: number;
  zoom?: number;
  grayscale?: boolean;
}

export const deviceResolutions = {
  'remarkable-paper-pro': { width: 2160, height: 1620 },
  'remarkable-paper-pro-move': { width: 1696, height: 954 }
} as const;

export type DeviceResolutionKey = keyof typeof deviceResolutions;

export async function prepareWallpaper(inputPath: string, deviceType: DeviceResolutionKey, options: WallpaperOptions): Promise<Buffer> {
  const resolution = deviceResolutions[deviceType];
  const image = sharp(inputPath);
  const metadata = await image.metadata();
  if (!metadata.width || !metadata.height) {
    throw new Error('Unable to read image dimensions');
  }
  const focusX = options.focusX ?? 0.5;
  const focusY = options.focusY ?? 0.5;
  const zoom = options.zoom ?? 1;

  if (options.mode === 'fill') {
    const scale = Math.max(resolution.width / metadata.width, resolution.height / metadata.height) * zoom;
    const scaledWidth = Math.round(metadata.width * scale);
    const scaledHeight = Math.round(metadata.height * scale);
    const left = Math.min(
      Math.max(Math.round((scaledWidth - resolution.width) * focusX), 0),
      Math.max(scaledWidth - resolution.width, 0)
    );
    const top = Math.min(
      Math.max(Math.round((scaledHeight - resolution.height) * focusY), 0),
      Math.max(scaledHeight - resolution.height, 0)
    );
    let pipeline = sharp(inputPath).resize(scaledWidth, scaledHeight).extract({
      left,
      top,
      width: resolution.width,
      height: resolution.height
    });
    if (options.grayscale !== false) {
      pipeline = pipeline.greyscale();
    }
    return pipeline.png().toBuffer();
  }

  let pipeline = sharp(inputPath).resize(resolution.width, resolution.height, {
    fit: 'contain',
    background: '#ffffff'
  });
  if (options.grayscale !== false) {
    pipeline = pipeline.greyscale();
  }
  return pipeline.png().toBuffer();
}

export interface WallpaperUploadResult {
  remotePath: string;
}

export async function uploadWallpaper(device: DeviceWithSecret, fileBuffer: Buffer, fileName: string): Promise<WallpaperUploadResult> {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rmtool-wallpaper-'));
  try {
    const localPath = path.join(tmpDir, fileName);
    fs.writeFileSync(localPath, fileBuffer);
    await withSftp(device, async (client) => {
      await client.put(localPath, path.join(device.wallpaperDir, fileName));
    });
    return { remotePath: path.join(device.wallpaperDir, fileName) };
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}
