declare interface Window {
  rmtool?: {
    getServerInfo: () => Promise<{ port: number }>;
  };
}
