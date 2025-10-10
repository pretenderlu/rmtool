import fs from 'fs';
import path from 'path';
import os from 'os';
import { randomUUID } from 'crypto';
import { PDFDocument } from 'pdf-lib';
import SftpClient from 'ssh2-sftp-client';
import { DeviceWithSecret } from '../config/deviceStore.js';
import { withSftp, executeCommand } from './sshClient.js';

const previewRoot = path.resolve('cache', 'thumbnails');

function ensurePreviewRoot() {
  fs.mkdirSync(previewRoot, { recursive: true });
}

export interface RemoteDocumentSummary {
  id: string;
  name: string;
  type: string;
  modified: number;
  hasPreview: boolean;
}

export async function listDocuments(device: DeviceWithSecret): Promise<RemoteDocumentSummary[]> {
  ensurePreviewRoot();
  return withSftp(device, async (client) => {
    const entries = await client.list(device.xochitlDir);
    const metadataFiles = entries.filter((entry) => entry.name.endsWith('.metadata'));
    const summaries: RemoteDocumentSummary[] = [];
    for (const entry of metadataFiles) {
      const id = entry.name.replace('.metadata', '');
      try {
        const metadataRaw = await client.get(path.join(device.xochitlDir, `${id}.metadata`));
        const metadata = JSON.parse(metadataRaw.toString('utf-8'));
        const contentRaw = await client.get(path.join(device.xochitlDir, `${id}.content`)).catch(() => null);
        const content = contentRaw ? JSON.parse(contentRaw.toString('utf-8')) : {};
        const modified = Number(metadata.lastModified ?? Date.now());
        const hasPreview = await ensureDocumentPreview(device, id, client);
        summaries.push({
          id,
          name: metadata.visibleName ?? id,
          type: content.fileType ?? 'notebook',
          modified,
          hasPreview
        });
      } catch (error) {
        console.error(`Failed to parse metadata for ${id}`, error);
      }
    }
    return summaries.sort((a, b) => b.modified - a.modified);
  });
}

export interface UploadResult {
  id: string;
  name: string;
}

export async function uploadDocument(device: DeviceWithSecret, localPath: string, originalName: string): Promise<UploadResult> {
  const ext = path.extname(originalName).toLowerCase();
  if (!['.pdf', '.epub'].includes(ext)) {
    throw new Error('Only PDF and EPUB files are supported');
  }
  const uuid = randomUUID();
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rmtool-'));
  try {
    const deviceFileName = `${uuid}${ext}`;
    fs.copyFileSync(localPath, path.join(tmpDir, deviceFileName));
    const metadata = {
      deleted: false,
      lastModified: `${Math.floor(Date.now() / 1000)}000`,
      metadatamodified: false,
      modified: false,
      parent: '',
      pinned: false,
      synced: false,
      type: 'DocumentType',
      version: 1,
      visibleName: path.parse(originalName).name
    };
    fs.writeFileSync(path.join(tmpDir, `${uuid}.metadata`), JSON.stringify(metadata, null, 2));
    if (ext === '.pdf') {
      const content = {
        extraMetadata: {},
        fileType: 'pdf',
        fontName: '',
        lastOpenedPage: 0,
        lineHeight: -1,
        margins: 100,
        pageCount: 1,
        textScale: 1,
        transform: {
          m11: 1,
          m12: 1,
          m13: 1,
          m21: 1,
          m22: 1,
          m23: 1,
          m31: 1,
          m32: 1,
          m33: 1
        }
      };
      fs.writeFileSync(path.join(tmpDir, `${uuid}.content`), JSON.stringify(content, null, 2));
      fs.mkdirSync(path.join(tmpDir, `${uuid}.cache`));
      fs.mkdirSync(path.join(tmpDir, `${uuid}.highlights`));
      fs.mkdirSync(path.join(tmpDir, `${uuid}.thumbnails`));
    } else {
      const content = {
        fileType: 'epub'
      };
      fs.writeFileSync(path.join(tmpDir, `${uuid}.content`), JSON.stringify(content, null, 2));
    }
    await withSftp(device, async (client) => {
      const files = fs.readdirSync(tmpDir);
      for (const file of files) {
        const localFile = path.join(tmpDir, file);
        const remoteFile = path.join(device.xochitlDir, file);
        await client.put(localFile, remoteFile);
      }
    });
    await executeCommand(device, 'systemctl restart xochitl');
    return { id: uuid, name: metadata.visibleName };
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

export interface ExportResult {
  path: string;
  pages: number;
}

export async function exportNoteToPdf(device: DeviceWithSecret, documentId: string, destination: string): Promise<ExportResult> {
  ensurePreviewRoot();
  return withSftp(device, async (client) => {
    const thumbnailsDir = path.join(device.xochitlDir, `${documentId}.thumbnails`);
    const exists = await client.exists(thumbnailsDir);
    if (!exists) {
      throw new Error('Thumbnails not found on device; open the note once to generate previews.');
    }
    const remoteThumbnails = await client.list(thumbnailsDir);
    const ordered = remoteThumbnails
      .filter((entry) => entry.type === '-')
      .filter((entry) => entry.name.endsWith('.png') || entry.name.endsWith('.jpg'))
      .sort((a, b) => a.name.localeCompare(b.name));
    if (ordered.length === 0) {
      throw new Error('No preview images available for this note');
    }
    const pdf = await PDFDocument.create();
    for (const thumb of ordered) {
      const buffer = await client.get(path.join(thumbnailsDir, thumb.name));
      const ext = path.extname(thumb.name).toLowerCase();
      if (ext === '.png') {
        const image = await pdf.embedPng(buffer as Buffer);
        const page = pdf.addPage([image.width, image.height]);
        page.drawImage(image, { x: 0, y: 0, width: image.width, height: image.height });
      } else {
        const image = await pdf.embedJpg(buffer as Buffer);
        const page = pdf.addPage([image.width, image.height]);
        page.drawImage(image, { x: 0, y: 0, width: image.width, height: image.height });
      }
    }
    const pdfBytes = await pdf.save();
    fs.writeFileSync(destination, Buffer.from(pdfBytes));
    return { path: destination, pages: ordered.length };
  });
}

export function getPreviewPath(deviceId: string, docId: string): string {
  ensurePreviewRoot();
  return path.join(previewRoot, `${deviceId}-${docId}.png`);
}

export async function ensureDocumentPreview(device: DeviceWithSecret, docId: string, existingClient?: SftpClient): Promise<boolean> {
  ensurePreviewRoot();
  const previewPath = getPreviewPath(device.id, docId);
  if (fs.existsSync(previewPath)) {
    return true;
  }
  const fetchPreview = async (client: SftpClient) => {
    const thumbnailsDir = path.join(device.xochitlDir, `${docId}.thumbnails`);
    const exists = await client.exists(thumbnailsDir);
    if (!exists) {
      return false;
    }
    const remoteThumbnails = await client.list(thumbnailsDir);
    const first = remoteThumbnails
      .filter((entry) => entry.type === '-')
      .find((entry) => entry.name.endsWith('.png') || entry.name.endsWith('.jpg'));
    if (!first) {
      return false;
    }
    const buffer = await client.get(path.join(thumbnailsDir, first.name));
    fs.writeFileSync(previewPath, buffer as Buffer);
    return true;
  };
  if (existingClient) {
    return fetchPreview(existingClient);
  }
  return withSftp(device, (client) => fetchPreview(client));
}
