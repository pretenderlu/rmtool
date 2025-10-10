import React, { useCallback, useEffect, useMemo, useState } from 'react';

interface DeviceProfile {
  id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  xochitlDir: string;
  wallpaperDir: string;
  deviceType: 'remarkable-paper-pro' | 'remarkable-paper-pro-move';
  connectTimeout: number;
  hasPassword: boolean;
}

interface DocumentSummary {
  id: string;
  name: string;
  type: string;
  modified: number;
  hasPreview: boolean;
}

const DEVICE_LABELS: Record<DeviceProfile['deviceType'], string> = {
  'remarkable-paper-pro': 'reMarkable Paper Pro',
  'remarkable-paper-pro-move': 'reMarkable Paper Pro Move'
};

const defaultForm: Partial<DeviceProfile> & { password?: string } = {
  name: '',
  host: '10.11.99.1',
  port: 22,
  username: 'root',
  xochitlDir: '/home/root/.local/share/remarkable/xochitl',
  wallpaperDir: '/usr/share/remarkable/',
  deviceType: 'remarkable-paper-pro',
  connectTimeout: 10000
};

const App: React.FC = () => {
  const [apiBase, setApiBase] = useState('');
  const [devices, setDevices] = useState<DeviceProfile[]>([]);
  const [selected, setSelected] = useState<DeviceProfile | null>(null);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [deviceForm, setDeviceForm] = useState(defaultForm);
  const [isEditing, setIsEditing] = useState(false);
  const [isLoadingDocuments, setIsLoadingDocuments] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  useEffect(() => {
    const resolvePort = async () => {
      if (window.rmtool) {
        const info = await window.rmtool.getServerInfo();
        setApiBase(`http://127.0.0.1:${info.port}`);
      } else {
        setApiBase('');
      }
    };
    resolvePort().catch(() => setApiBase(''));
  }, []);

  const fetchJson = useCallback(
    async (input: RequestInfo, init?: RequestInit) => {
      const response = await fetch(input, init);
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || '请求失败');
      }
      return response.json();
    },
    []
  );

  const loadDevices = useCallback(async () => {
    const data: DeviceProfile[] = await fetchJson(`${apiBase}/api/devices`);
    setDevices(data);
    setSelected((prev) => {
      if (prev) {
        const next = data.find((item) => item.id === prev.id);
        if (next) {
          return next;
        }
      }
      return data.length > 0 ? data[0] : null;
    });
  }, [apiBase, fetchJson]);

  useEffect(() => {
    if (apiBase !== null) {
      loadDevices().catch((error) => setStatusMessage(error.message));
    }
  }, [apiBase, loadDevices]);

  const refreshDocuments = useCallback(
    async (deviceId: string) => {
      setIsLoadingDocuments(true);
      try {
        const data: DocumentSummary[] = await fetchJson(`${apiBase}/api/devices/${deviceId}/documents`);
        setDocuments(data);
      } catch (error) {
        if (error instanceof Error) {
          setStatusMessage(error.message);
        }
      } finally {
        setIsLoadingDocuments(false);
      }
    },
    [apiBase, fetchJson]
  );

  useEffect(() => {
    if (!selected) {
      setDocuments([]);
      return;
    }
    refreshDocuments(selected.id).catch(() => undefined);
  }, [refreshDocuments, selected]);

  const handleDeviceSelect = (device: DeviceProfile) => {
    setSelected(device);
    setIsEditing(false);
    setDeviceForm(defaultForm);
  };

  const handleDeviceSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!deviceForm.name || !deviceForm.host || !deviceForm.username) {
      setStatusMessage('请填写完整的设备信息');
      return;
    }
    const payload = {
      name: deviceForm.name,
      host: deviceForm.host,
      port: deviceForm.port,
      username: deviceForm.username,
      password: deviceForm.password,
      xochitlDir: deviceForm.xochitlDir,
      wallpaperDir: deviceForm.wallpaperDir,
      deviceType: deviceForm.deviceType,
      connectTimeout: deviceForm.connectTimeout
    };
    try {
      if (isEditing && selected) {
        await fetchJson(`${apiBase}/api/devices/${selected.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } else {
        await fetchJson(`${apiBase}/api/devices`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      }
      setStatusMessage('设备信息已保存');
      setDeviceForm(defaultForm);
      setIsEditing(false);
      await loadDevices();
    } catch (error) {
      if (error instanceof Error) {
        setStatusMessage(error.message);
      }
    }
  };

  const handleDeleteDevice = async (device: DeviceProfile) => {
    if (!window.confirm(`确定删除设备 ${device.name} 吗？`)) {
      return;
    }
    try {
      await fetchJson(`${apiBase}/api/devices/${device.id}`, { method: 'DELETE' });
      setStatusMessage('设备已删除');
      await loadDevices();
    } catch (error) {
      if (error instanceof Error) {
        setStatusMessage(error.message);
      }
    }
  };

  const handleUploadDocument = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!selected || !event.target.files?.length) {
      return;
    }
    const file = event.target.files[0];
    const formData = new FormData();
    formData.append('document', file);
    try {
      setStatusMessage(`正在上传 ${file.name}`);
      await fetch(`${apiBase}/api/devices/${selected.id}/documents`, {
        method: 'POST',
        body: formData
      }).then((response) => {
        if (!response.ok) {
          throw new Error('上传失败');
        }
      });
      setStatusMessage('上传成功');
      const deviceId = selected.id;
      await loadDevices();
      await refreshDocuments(deviceId);
    } catch (error) {
      if (error instanceof Error) {
        setStatusMessage(error.message);
      }
    } finally {
      event.target.value = '';
    }
  };

  const handleUploadWallpaper = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!selected || !event.target.files?.length) {
      return;
    }
    const file = event.target.files[0];
    const formData = new FormData();
    formData.append('wallpaper', file);
    formData.append('mode', 'fill');
    try {
      setStatusMessage('正在处理壁纸');
      await fetch(`${apiBase}/api/devices/${selected.id}/wallpapers`, {
        method: 'POST',
        body: formData
      }).then((response) => {
        if (!response.ok) {
          throw new Error('壁纸上传失败');
        }
      });
      setStatusMessage('壁纸已上传');
    } catch (error) {
      if (error instanceof Error) {
        setStatusMessage(error.message);
      }
    } finally {
      event.target.value = '';
    }
  };

  const handleExport = async (doc: DocumentSummary) => {
    if (!selected) {
      return;
    }
    try {
      setStatusMessage('正在生成 PDF 预览');
      const response = await fetch(`${apiBase}/api/devices/${selected.id}/documents/${doc.id}/export`, {
        method: 'POST'
      });
      if (!response.ok) {
        throw new Error('导出失败');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${doc.name}.pdf`;
      anchor.click();
      URL.revokeObjectURL(url);
      setStatusMessage('PDF 已生成');
    } catch (error) {
      if (error instanceof Error) {
        setStatusMessage(error.message);
      }
    }
  };

  const sortedDevices = useMemo(() => [...devices].sort((a, b) => a.name.localeCompare(b.name)), [devices]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-950 to-black text-slate-50">
      <header className="border-b border-white/10 backdrop-blur bg-white/5">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">rmtool 控制台</h1>
            <p className="text-sm text-slate-300">管理多台 reMarkable 设备、壁纸与文档</p>
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-300">
            <span className="inline-flex items-center gap-2 rounded-full bg-green-500/10 px-3 py-1 text-green-300">
              <span className="h-2 w-2 rounded-full bg-green-400" />
              API {apiBase ? apiBase : '本地'}
            </span>
            {statusMessage && <span className="text-sky-300">{statusMessage}</span>}
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-6 lg:flex-row">
        <section className="w-full max-w-xs rounded-3xl border border-white/10 bg-white/5 p-6 shadow-lg backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">设备列表</h2>
            <button
              className="rounded-full bg-primary/20 px-3 py-1 text-xs font-semibold text-primary transition hover:bg-primary/30"
              onClick={() => {
                setIsEditing(false);
                setDeviceForm(defaultForm);
              }}
            >
              新增
            </button>
          </div>
          <ul className="mt-4 space-y-2">
            {sortedDevices.map((device) => (
              <li key={device.id}>
                <button
                  className={`w-full rounded-2xl border px-4 py-3 text-left transition ${
                    selected?.id === device.id
                      ? 'border-primary/60 bg-primary/10 text-primary'
                      : 'border-white/5 bg-black/10 hover:border-white/20'
                  }`}
                  onClick={() => handleDeviceSelect(device)}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-semibold">{device.name}</span>
                    <span className="text-xs uppercase tracking-wide text-slate-300">
                      {DEVICE_LABELS[device.deviceType]}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {device.username}@{device.host}:{device.port}
                  </p>
                  <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400">
                    <button
                      className="rounded-full border border-white/10 px-2 py-1 hover:border-white/40"
                      onClick={(event) => {
                        event.stopPropagation();
                        setIsEditing(true);
                        setDeviceForm({ ...device, password: '' });
                        setSelected(device);
                      }}
                    >
                      编辑
                    </button>
                    <button
                      className="rounded-full border border-red-500/40 px-2 py-1 text-red-200 hover:bg-red-500/10"
                      onClick={(event) => {
                        event.stopPropagation();
                        void handleDeleteDevice(device);
                      }}
                    >
                      删除
                    </button>
                  </div>
                </button>
              </li>
            ))}
            {sortedDevices.length === 0 && (
              <li className="text-sm text-slate-400">尚未配置设备</li>
            )}
          </ul>
        </section>

        <section className="flex-1 space-y-6">
          <div className="rounded-3xl border border-white/10 bg-white/5 p-6 shadow-lg backdrop-blur">
            <h2 className="text-lg font-semibold">{isEditing ? '编辑设备' : '新增设备'}</h2>
            <form className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={handleDeviceSubmit}>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">设备名称</span>
                <input
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.name ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, name: event.target.value }))}
                  required
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">IP 地址</span>
                <input
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.host ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, host: event.target.value }))}
                  required
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">端口</span>
                <input
                  type="number"
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.port ?? 22}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, port: Number(event.target.value) }))}
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">用户名</span>
                <input
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.username ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, username: event.target.value }))}
                  required
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">密码</span>
                <input
                  type="password"
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.password ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, password: event.target.value }))}
                  placeholder={isEditing ? '留空则保持不变' : '如使用 SSH 密钥可留空'}
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1 text-slate-300">设备类型</span>
                <select
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.deviceType}
                  onChange={(event) =>
                    setDeviceForm((prev) => ({ ...prev, deviceType: event.target.value as DeviceProfile['deviceType'] }))
                  }
                >
                  {Object.entries(DEVICE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col text-sm md:col-span-2">
                <span className="mb-1 text-slate-300">Xochitl 路径</span>
                <input
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.xochitlDir ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, xochitlDir: event.target.value }))}
                />
              </label>
              <label className="flex flex-col text-sm md:col-span-2">
                <span className="mb-1 text-slate-300">壁纸目录</span>
                <input
                  className="rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus:border-primary focus:outline-none"
                  value={deviceForm.wallpaperDir ?? ''}
                  onChange={(event) => setDeviceForm((prev) => ({ ...prev, wallpaperDir: event.target.value }))}
                />
              </label>
              <div className="flex items-center justify-end gap-3 md:col-span-2">
                <button
                  type="button"
                  className="rounded-xl border border-white/10 px-4 py-2 text-sm text-slate-200 hover:border-white/40"
                  onClick={() => {
                    setDeviceForm(defaultForm);
                    setIsEditing(false);
                  }}
                >
                  重置
                </button>
                <button
                  type="submit"
                  className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/30 transition hover:translate-y-[-1px]"
                >
                  {isEditing ? '保存修改' : '添加设备'}
                </button>
              </div>
            </form>
          </div>

          <div className="rounded-3xl border border-white/10 bg-white/5 p-6 shadow-lg backdrop-blur">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">文档管理</h2>
                <p className="text-sm text-slate-300">上传 PDF/EPUB、预览笔记缩略图并导出为 PDF</p>
              </div>
              <div className="flex flex-wrap gap-3 text-sm">
                <label className="cursor-pointer rounded-xl border border-white/10 bg-black/30 px-4 py-2 transition hover:border-primary/60">
                  上传文档
                  <input type="file" accept=".pdf,.epub" className="hidden" onChange={handleUploadDocument} />
                </label>
                <label className="cursor-pointer rounded-xl border border-white/10 bg-black/30 px-4 py-2 transition hover:border-primary/60">
                  设置壁纸
                  <input type="file" accept="image/*" className="hidden" onChange={handleUploadWallpaper} />
                </label>
              </div>
            </div>
            <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {isLoadingDocuments && <p className="text-slate-400">正在加载文档...</p>}
              {!isLoadingDocuments && documents.length === 0 && (
                <p className="rounded-2xl border border-dashed border-white/10 p-6 text-sm text-slate-400">
                  暂无文档，尝试上传 PDF 或 EPUB 文件。
                </p>
              )}
              {documents.map((doc) => (
                <article
                  key={doc.id}
                  className="group relative overflow-hidden rounded-3xl border border-white/10 bg-black/30 transition hover:border-primary/40 hover:shadow-xl hover:shadow-primary/10"
                >
                  <div className="aspect-[3/4] w-full bg-gradient-to-br from-slate-800 to-slate-950">
                    {doc.hasPreview ? (
                      <img
                        src={`${apiBase}/api/devices/${selected?.id}/documents/${doc.id}/preview?${doc.modified}`}
                        alt={doc.name}
                        className="h-full w-full object-cover"
                      />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-slate-500">
                        无预览
                      </div>
                    )}
                  </div>
                  <div className="space-y-2 px-4 py-3">
                    <h3 className="text-base font-semibold text-white">{doc.name}</h3>
                    <div className="flex items-center justify-between text-xs text-slate-400">
                      <span>{doc.type.toUpperCase()}</span>
                      <time>{new Date(doc.modified).toLocaleString()}</time>
                    </div>
                    <button
                      className="w-full rounded-xl border border-primary/40 px-3 py-2 text-sm font-semibold text-primary transition group-hover:bg-primary/10"
                      onClick={() => void handleExport(doc)}
                    >
                      导出为 PDF
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
};

export default App;
