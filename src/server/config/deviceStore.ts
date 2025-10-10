import fs from 'fs';
import path from 'path';
import { randomUUID } from 'crypto';
import { z } from 'zod';
import { SecretStore } from '../services/secretStore.js';

const devicesFile = path.resolve('data', 'devices.json');

const deviceSchema = z.object({
  id: z.string(),
  name: z.string(),
  host: z.string(),
  port: z.number().int().nonnegative(),
  username: z.string(),
  xochitlDir: z.string(),
  wallpaperDir: z.string(),
  deviceType: z.enum(['remarkable-paper-pro', 'remarkable-paper-pro-move']),
  connectTimeout: z.number().int().positive().default(10000),
  hasPassword: z.boolean().default(false)
});

export type DeviceProfile = z.infer<typeof deviceSchema>;

export interface DeviceWithSecret extends DeviceProfile {
  password?: string;
}

export interface CreateDeviceInput {
  name: string;
  host: string;
  port?: number;
  username: string;
  password?: string;
  xochitlDir?: string;
  wallpaperDir?: string;
  deviceType: DeviceProfile['deviceType'];
  connectTimeout?: number;
}

export interface UpdateDeviceInput extends Partial<CreateDeviceInput> {}

export class DeviceStore {
  private readonly secretStore: SecretStore;

  constructor(secretStore = new SecretStore()) {
    this.secretStore = secretStore;
    if (!fs.existsSync(devicesFile)) {
      fs.mkdirSync(path.dirname(devicesFile), { recursive: true });
      fs.writeFileSync(devicesFile, JSON.stringify([]));
    }
  }

  private read(): DeviceProfile[] {
    const raw = fs.readFileSync(devicesFile, 'utf-8');
    const parsed = JSON.parse(raw || '[]');
    return z.array(deviceSchema).parse(parsed);
  }

  private write(devices: DeviceProfile[]) {
    fs.writeFileSync(devicesFile, JSON.stringify(devices, null, 2));
  }

  list(): DeviceProfile[] {
    return this.read();
  }

  get(id: string): DeviceProfile | undefined {
    return this.read().find((device) => device.id === id);
  }

  async getWithSecret(id: string): Promise<DeviceWithSecret | undefined> {
    const device = this.get(id);
    if (!device) {
      return undefined;
    }
    const password = await this.secretStore.getSecret(id);
    return { ...device, password: password ?? undefined };
  }

  async create(input: CreateDeviceInput): Promise<DeviceProfile> {
    const devices = this.read();
    const id = randomUUID();
    const profile: DeviceProfile = {
      id,
      name: input.name,
      host: input.host,
      port: input.port ?? 22,
      username: input.username,
      xochitlDir: input.xochitlDir ?? '/home/root/.local/share/remarkable/xochitl',
      wallpaperDir: input.wallpaperDir ?? '/usr/share/remarkable/',
      deviceType: input.deviceType,
      connectTimeout: input.connectTimeout ?? 10000,
      hasPassword: Boolean(input.password)
    };
    devices.push(profile);
    this.write(devices);
    if (input.password) {
      await this.secretStore.storeSecret(id, input.password);
    }
    return profile;
  }

  async update(id: string, input: UpdateDeviceInput): Promise<DeviceProfile> {
    const devices = this.read();
    const index = devices.findIndex((device) => device.id === id);
    if (index === -1) {
      throw new Error(`Device ${id} not found`);
    }
    const current = devices[index];
    const updated: DeviceProfile = {
      ...current,
      ...('name' in input ? { name: input.name! } : {}),
      ...('host' in input ? { host: input.host! } : {}),
      ...('port' in input && input.port ? { port: input.port } : {}),
      ...('username' in input ? { username: input.username! } : {}),
      ...('xochitlDir' in input ? { xochitlDir: input.xochitlDir ?? current.xochitlDir } : {}),
      ...('wallpaperDir' in input ? { wallpaperDir: input.wallpaperDir ?? current.wallpaperDir } : {}),
      ...('deviceType' in input && input.deviceType ? { deviceType: input.deviceType } : {}),
      ...('connectTimeout' in input && input.connectTimeout ? { connectTimeout: input.connectTimeout } : {})
    } as DeviceProfile;
    if (input.password !== undefined) {
      updated.hasPassword = input.password.length > 0;
      if (input.password.length > 0) {
        await this.secretStore.storeSecret(id, input.password);
      } else {
        await this.secretStore.deleteSecret(id);
      }
    }
    devices[index] = updated;
    this.write(devices);
    return updated;
  }

  async delete(id: string): Promise<void> {
    const devices = this.read().filter((device) => device.id !== id);
    this.write(devices);
    await this.secretStore.deleteSecret(id);
  }
}
