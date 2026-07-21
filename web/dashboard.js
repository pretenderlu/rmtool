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
        // Theme switching: variables live in dashboard.css under
        // :root (dark) and [data-theme="light"]; here we only flip the flag.
        if (state.theme === "light" || state.theme === "dark") {
            document.documentElement.dataset.theme = state.theme;
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
