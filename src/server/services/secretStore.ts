import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

const secretsFile = path.resolve('data', 'secrets.json');
const keyFile = path.resolve('data', '.secret');

function ensureSecretsFile() {
  fs.mkdirSync(path.dirname(secretsFile), { recursive: true });
  if (!fs.existsSync(secretsFile)) {
    fs.writeFileSync(secretsFile, JSON.stringify({}));
  }
}

function readSecrets(): Record<string, string> {
  ensureSecretsFile();
  const raw = fs.readFileSync(secretsFile, 'utf-8');
  return JSON.parse(raw || '{}');
}

function writeSecrets(secrets: Record<string, string>) {
  ensureSecretsFile();
  fs.writeFileSync(secretsFile, JSON.stringify(secrets, null, 2));
}

function deriveKey(): Buffer {
  const envKey = process.env.RMTOOL_SECRET_KEY;
  if (envKey) {
    const buf = Buffer.from(envKey, 'base64');
    if (buf.length !== 32) {
      throw new Error('RMTOOL_SECRET_KEY must be a base64 encoded 32-byte key');
    }
    return buf;
  }
  if (fs.existsSync(keyFile)) {
    return Buffer.from(fs.readFileSync(keyFile, 'utf-8'), 'base64');
  }
  const key = crypto.randomBytes(32);
  fs.writeFileSync(keyFile, key.toString('base64'));
  return key;
}

export class SecretStore {
  private readonly key: Buffer;

  constructor() {
    this.key = deriveKey();
  }

  async storeSecret(id: string, secret: string): Promise<void> {
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', this.key, iv);
    const encrypted = Buffer.concat([cipher.update(secret, 'utf-8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    const payload = Buffer.concat([iv, tag, encrypted]).toString('base64');
    const secrets = readSecrets();
    secrets[id] = payload;
    writeSecrets(secrets);
  }

  async getSecret(id: string): Promise<string | null> {
    const secrets = readSecrets();
    const payload = secrets[id];
    if (!payload) {
      return null;
    }
    const buffer = Buffer.from(payload, 'base64');
    const iv = buffer.subarray(0, 12);
    const tag = buffer.subarray(12, 28);
    const encrypted = buffer.subarray(28);
    const decipher = crypto.createDecipheriv('aes-256-gcm', this.key, iv);
    decipher.setAuthTag(tag);
    const decrypted = Buffer.concat([decipher.update(encrypted), decipher.final()]);
    return decrypted.toString('utf-8');
  }

  async deleteSecret(id: string): Promise<void> {
    const secrets = readSecrets();
    if (secrets[id]) {
      delete secrets[id];
      writeSecrets(secrets);
    }
  }
}
