import { Client as SSHClient } from 'ssh2';
import SftpClient from 'ssh2-sftp-client';
import { DeviceWithSecret } from '../config/deviceStore.js';

export interface RemoteCommandResult {
  stdout: string;
  stderr: string;
}

export async function withSftp<T>(device: DeviceWithSecret, fn: (client: SftpClient) => Promise<T>): Promise<T> {
  const sftp = new SftpClient();
  const connection = {
    host: device.host,
    port: device.port,
    username: device.username,
    password: device.password,
    readyTimeout: device.connectTimeout
  };
  await sftp.connect(connection);
  try {
    return await fn(sftp);
  } finally {
    await sftp.end();
  }
}

export async function executeCommand(device: DeviceWithSecret, command: string): Promise<RemoteCommandResult> {
  const client = new SSHClient();
  const connection = {
    host: device.host,
    port: device.port,
    username: device.username,
    password: device.password,
    readyTimeout: device.connectTimeout
  };
  return new Promise<RemoteCommandResult>((resolve, reject) => {
    client
      .on('ready', () => {
        client.exec(command, (err, stream) => {
          if (err) {
            client.end();
            reject(err);
            return;
          }
          let stdout = '';
          let stderr = '';
          stream
            .on('close', () => {
              client.end();
              resolve({ stdout, stderr });
            })
            .on('data', (data: Buffer) => {
              stdout += data.toString();
            })
            .stderr.on('data', (data: Buffer) => {
              stderr += data.toString();
            });
        });
      })
      .on('error', (error) => {
        reject(error);
      })
      .connect(connection);
  });
}
