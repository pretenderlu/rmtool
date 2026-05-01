(function () {
    const $ = (id) => document.getElementById(id);

    const setText = (id, value, fallback = "—") => {
        const el = $(id);
        if (!el) {
            return;
        }
        const resolved = value === undefined || value === null || value === "" ? fallback : value;
        el.textContent = resolved;
    };

    const renderActions = (connected, totalDocs) => {
        const list = $("next-actions");
        if (!list) {
            return;
        }
        const items = [];
        if (!connected) {
            items.push("请在顶部连接面板中选择设备并输入 root 密码以建立连接。");
            items.push("连接成功后可一键刷新文档、上传字体以及壁纸。");
        } else {
            if (!totalDocs) {
                items.push("当前设备暂无文档，点击“上传文档”开始传输 PDF 或 EPUB 文件。");
            } else {
                items.push("在“文档预览/上传”标签中查看文档详情与缩略图预览，必要时继续上传新文件。");
            }
            items.push("在“壁纸管理”页选择图片，系统会按设备分辨率自动裁剪并提供预览。");
        }
        list.innerHTML = items.map((item) => `<li>${item}</li>`).join("");
    };

    window.updateDashboard = (state = {}) => {
        // Theme switching via CSS custom properties
        if (state.theme === "light") {
            const root = document.documentElement;
            root.style.setProperty("color-scheme", "light");
            root.style.setProperty("--bg-base", "#F8FAFC");
            root.style.setProperty("--bg-card", "rgba(255, 255, 255, 0.85)");
            root.style.setProperty("--bg-card-inner", "rgba(0, 0, 0, 0.02)");
            root.style.setProperty("--border", "rgba(0, 0, 0, 0.08)");
            root.style.setProperty("--border-accent", "rgba(37, 99, 235, 0.3)");
            root.style.setProperty("--accent", "#2563EB");
            root.style.setProperty("--accent-cyan", "#0284C7");
            root.style.setProperty("--text", "#0F172A");
            root.style.setProperty("--muted", "#64748B");
            root.style.setProperty("--green", "#059669");
            root.style.setProperty("--red", "#DC2626");
            document.body.style.background = `
                radial-gradient(ellipse at 15% 20%, rgba(37, 99, 235, 0.08), transparent 45%),
                radial-gradient(ellipse at 85% 15%, rgba(2, 132, 199, 0.05), transparent 40%),
                radial-gradient(ellipse at 50% 90%, rgba(37, 99, 235, 0.04), transparent 50%),
                var(--bg-base)
            `;
        } else if (state.theme === "dark") {
            const root = document.documentElement;
            root.style.setProperty("color-scheme", "dark");
            root.style.setProperty("--bg-base", "#0B0E14");
            root.style.setProperty("--bg-card", "rgba(22, 26, 38, 0.75)");
            root.style.setProperty("--bg-card-inner", "rgba(255, 255, 255, 0.03)");
            root.style.setProperty("--border", "rgba(255, 255, 255, 0.06)");
            root.style.setProperty("--border-accent", "rgba(99, 102, 241, 0.4)");
            root.style.setProperty("--accent", "#6366F1");
            root.style.setProperty("--accent-cyan", "#06B6D4");
            root.style.setProperty("--text", "#F8FAFC");
            root.style.setProperty("--muted", "#94A3B8");
            root.style.setProperty("--green", "#10B981");
            root.style.setProperty("--red", "#EF4444");
            document.body.style.background = "";
        }

        const connected = Boolean(state.connected);
        const badge = $("connection-badge");
        if (badge) {
            badge.textContent = connected ? "已连接" : "未连接";
            badge.classList.toggle("status-online", connected);
            badge.classList.toggle("status-offline", !connected);
        }

        const device = state.device || {};
        setText("device-name", device.name);
        setText("device-type", device.type);
        setText("device-mode", device.mode === "wifi" ? "Wi-Fi" : device.mode === "usb" ? "USB" : device.mode);
        setText("device-host", device.host);
        setText("connection-updated", state.lastConnectionChange);

        const docs = state.documents || {};
        setText("doc-total", docs.total ?? 0, "0");
        setText("doc-pdf", docs.pdf ?? 0, "0");
        setText("doc-epub", docs.epub ?? 0, "0");
        setText("doc-note", docs.notes ?? 0, "0");
        setText("doc-updated", docs.lastUpdated);

        renderActions(connected, docs.total ?? 0);
    };

    window.updateDashboard({});
})();
