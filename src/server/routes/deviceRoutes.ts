import fs from 'fs';
import path from 'path';
import os from 'os';
import { Router } from 'express';
import multer from 'multer';
import { z } from 'zod';
import { DeviceStore } from '../config/deviceStore.js';
import { listDocuments, uploadDocument, exportNoteToPdf, getPreviewPath } from '../services/documentManager.js';
import { deviceResolutions, prepareWallpaper, uploadWallpaper } from '../services/wallpaperManager.js';

const router = Router();
const upload = multer({ dest: path.join(os.tmpdir(), 'rmtool-upload-') });
const store = new DeviceStore();

const deviceInputSchema = z.object({
  name: z.string(),
  host: z.string(),
  port: z.coerce.number().optional(),
  username: z.string(),
  password: z.string().optional(),
  xochitlDir: z.string().optional(),
  wallpaperDir: z.string().optional(),
  deviceType: z.enum(['remarkable-paper-pro', 'remarkable-paper-pro-move']),
  connectTimeout: z.coerce.number().optional()
});

router.get('/', (_req, res) => {
  res.json(store.list());
});

router.get('/:id', (req, res) => {
  const device = store.get(req.params.id);
  if (!device) {
    res.status(404).json({ error: 'Device not found' });
    return;
  }
  res.json(device);
});

router.post('/', async (req, res, next) => {
  try {
    const body = deviceInputSchema.parse(req.body);
    const device = await store.create(body);
    res.status(201).json(device);
  } catch (error) {
    next(error);
  }
});

router.put('/:id', async (req, res, next) => {
  try {
    const body = deviceInputSchema.partial().parse(req.body);
    const device = await store.update(req.params.id, body);
    res.json(device);
  } catch (error) {
    next(error);
  }
});

router.delete('/:id', async (req, res, next) => {
  try {
    await store.delete(req.params.id);
    res.status(204).end();
  } catch (error) {
    next(error);
  }
});

router.get('/:id/documents', async (req, res, next) => {
  try {
    const device = await store.getWithSecret(req.params.id);
    if (!device) {
      res.status(404).json({ error: 'Device not found' });
      return;
    }
    const documents = await listDocuments(device);
    res.json(documents);
  } catch (error) {
    next(error);
  }
});

router.post('/:id/documents', upload.single('document'), async (req, res, next) => {
  try {
    const device = await store.getWithSecret(req.params.id);
    if (!device) {
      res.status(404).json({ error: 'Device not found' });
      return;
    }
    if (!req.file) {
      res.status(400).json({ error: 'Document file is required' });
      return;
    }
    const result = await uploadDocument(device, req.file.path, req.file.originalname);
    res.status(201).json(result);
  } catch (error) {
    next(error);
  } finally {
    if (req.file) {
      fs.unlink(req.file.path, () => undefined);
    }
  }
});

const wallpaperSchema = z.object({
  mode: z.enum(['fill', 'fit']).default('fill'),
  focusX: z.coerce.number().min(0).max(1).optional(),
  focusY: z.coerce.number().min(0).max(1).optional(),
  zoom: z.coerce.number().min(0.5).max(3).optional(),
  grayscale: z.coerce.boolean().optional()
});

router.post('/:id/wallpapers', upload.single('wallpaper'), async (req, res, next) => {
  try {
    const device = await store.getWithSecret(req.params.id);
    if (!device) {
      res.status(404).json({ error: 'Device not found' });
      return;
    }
    if (!req.file) {
      res.status(400).json({ error: 'Wallpaper is required' });
      return;
    }
    const options = wallpaperSchema.parse(req.body);
    const buffer = await prepareWallpaper(req.file.path, device.deviceType, options);
    const fileName = `${Date.now()}-${req.file.originalname.replace(/\s+/g, '-')}.png`;
    const uploadResult = await uploadWallpaper(device, buffer, fileName);
    res.status(201).json({ ...uploadResult, resolution: deviceResolutions[device.deviceType] });
  } catch (error) {
    next(error);
  } finally {
    if (req.file) {
      fs.unlink(req.file.path, () => undefined);
    }
  }
});

router.get('/:id/documents/:docId/preview', async (req, res, next) => {
  try {
    const device = await store.getWithSecret(req.params.id);
    if (!device) {
      res.status(404).json({ error: 'Device not found' });
      return;
    }
    const previewPath = getPreviewPath(device.id, req.params.docId);
    if (!fs.existsSync(previewPath)) {
      res.status(404).json({ error: 'Preview not found' });
      return;
    }
    res.sendFile(previewPath);
  } catch (error) {
    next(error);
  }
});

router.post('/:id/documents/:docId/export', async (req, res, next) => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rmtool-export-'));
  const cleanup = () => fs.rmSync(tmpDir, { recursive: true, force: true });
  try {
    const device = await store.getWithSecret(req.params.id);
    if (!device) {
      cleanup();
      res.status(404).json({ error: 'Device not found' });
      return;
    }
    const pdfPath = path.join(tmpDir, `${req.params.docId}.pdf`);
    const result = await exportNoteToPdf(device, req.params.docId, pdfPath);
    res.download(result.path, `${req.params.docId}.pdf`, (downloadErr) => {
      cleanup();
      if (downloadErr) {
        next(downloadErr);
      }
    });
  } catch (error) {
    cleanup();
    next(error);
  }
});

export default router;
