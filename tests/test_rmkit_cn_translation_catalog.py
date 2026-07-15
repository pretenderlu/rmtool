import hashlib
import json
import re
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


import _rmkit_cn


EXPECTED_MESSAGES = 1847
MINIMUM_CONTEXTS = 300
STOCK_MESSAGES = 1779
STOCK_KEY_SHA256 = "6362ce405416df7b45faaee315f517ffb96f1c875814a623b5e776edafaf19cb"
STATIC_SUPPLEMENT_KEY_SHA256 = "6fc30d51bdf241e1f199534e161f749db98efcbab685e3f0ea1fa8a20a320d92"
FORBIDDEN_TYPES = {"unfinished", "obsolete", "vanished"}
PLACEHOLDER_RE = re.compile(r"%(?:L\d+|\d+|n)")
TAG_RE = re.compile(r"</?([A-Za-z][\w:.-]*)(?:\s[^<>]*?)?/?>")
URL_RE = re.compile(r"https?://[^\s<]+")
SUSPICIOUS_REPLACEMENT_RE = re.compile(r"\?{2,}")
SETTINGS_MODEL_TRANSLATIONS = {
    "General": "常规",
    "Wifi": "Wi-Fi",
    "Cloud": "云端",
    "Security": "安全",
    "Display": "显示",
    "Accessibility": "辅助功能",
    "Help": "帮助",
    "Developer": "开发者",
    "Experimental": "实验性功能",
}
PEN_COLOR_TRANSLATIONS = {"Magenta": "品红色"}
READING_LIGHT_CONTEXTS = ("DisplaySettingsHeader",)
READING_LIGHT_DESCRIPTION = (
    "Manage the brightness level in Quick settings by swiping down in the "
    "top-&#8288;right&#160;corner."
)
STATIC_SUPPLEMENT_KEYS = {
    ("Account", "%1 subscription", "Subscription info message", False),
    ("Account", "Enterprise", "Enterprise enrollment message", False),
    ("Account", "Log in to manage your subscription at %1.", "Subscription settings info message", False),
    ("Account", "Subscribe to Connect, with unlimited cloud storage and sync, and access to all note-taking features in the mobile and desktop apps.", "Cloud storage information message (no connect)", False),
    ("Account", "This will delete all files stored on your paper tablet and restore it to its original settings.", "Factory reset message", False),
    ("Account", "You have unlimited cloud storage, sync, and access to all note-taking features in the mobile and desktop apps.", "Cloud storage information message (connect)", False),
    ("Account", "Your workplace has set certain policies to strengthen the security on the device.", "Enterprise enrollment information message", False),
    ("ActionHeader", "Choose a placement", "Move document, moving allowed", False),
    ("ActionHeader", "Select where to move the item", "Move document, moving NOT allowed", False),
    ("ActionHeader", "Select where to move the items", "Move multiple documents, moving NOT allowed", False),
    ("Cloud", "Log in to manage your subscription at %1.", "Subscription settings info message", False),
    ("ConfirmDeleteTagsWindow", "Cancel", "cancel delete", False),
    ("ConfirmDeleteTagsWindow", "Delete", "confirm delete", False),
    ("ConfirmDeleteTrashWindow", "Cancel", "cancel delete", False),
    ("ConfirmDeleteTrashWindow", "Delete", "confirm delete", False),
    ("CreateCollection", "Cancel", "top bar", False),
    ("CreateNotebook", "Cancel", "top bar", False),
    ("CreateNotebook", "Create", "Notebook creator", False),
    ("CreateNotebook", "New notebook", "Creates notebook", False),
    ("CreateNotebook", "Templates", "template chooser", False),
    ("CreateNotebook", "View all", "template chooser", False),
    ("EditDocument", "Cancel", "top bar", False),
    ("EditDocument", "Save", "Entry creator/editor modal", False),
    ("EmptyViewItem", "Create a reading list", "pdfs", False),
    ("EmptyViewItem", "Fill this with content", "notebooks", False),
    ("ExplorerSearch", "File size", "", False),
    ("ExplorerSearch", "Last opened", "", False),
    ("FileTypeSelectionFoldout", "PDF", "", False),
    ("GestureCheatSheet", "Marker", "", False),
    ("HandednessSelectPage", "Choose your %1writing hand%2", "words between %1 and %2 will be highlighted", False),
    ("MarkerCellItem", "Marker", "", False),
    ("MarkerSetupPageBase", "%1Attach%2 your Marker", "words between %1 and %2 will be highlighted", False),
    ("MarkerSetupPageBase", "Your Marker is %1charging%2", "words between %1 and %2 will be highlighted", False),
    (
        "MdmSshAboutText",
        "<p>To do so, this device acts as an USB ethernet device, and you can connect using the SSH protocol. \n"
        "Because this device is enrolled with your enterprise, SSH connectivity has been disabled for security purposes.</p>",
        "",
        False,
    ),
    ("Navigator", "Select where to place the page", "Move page in my files", False),
    ("OAuthSetupPage", "%1Pair%2 your device", "words between %1 and %2 will be highlighted", False),
    ("OAuthSetupPage", "%1Unable%2 to complete pairing", "words between %1 and %2 will be highlighted", False),
    ("OAuthSetupPage", "%1Verify%2 email to pair", "words between %1 and %2 will be highlighted", False),
    ("OAuthSetupPage", "Pair your device %1later%2", "words between %1 and %2 will be highlighted", False),
    ("OneTimeCodeSetupPage", "%1Pair%2 your device", "words between %1 and %2 will be highlighted", False),
    ("OneTimeCodeSetupPage", "%1Unable%2 to complete pairing", "words between %1 and %2 will be highlighted", False),
    ("OneTimeCodeSetupPage", "Pair your device %1later%2", "words between %1 and %2 will be highlighted", False),
    ("PairingReauthWindow", ", %1", "", False),
    ("PoweroffWindow", "Do you want to restart your reMarkable?", "Dialog: restart title", False),
    ("PoweroffWindow", "Do you want to turn off your reMarkable?", "Dialog: turn off title", False),
    ("RenameEntity", "Cancel", "Cancel renaming of folder or notebook", False),
    ("RenameEntity", "Rename", "Rename folder or notebook", False),
    ("RenameEntity", "Save", "Save renamed folder or notebook", False),
    (
        "RetailSshAboutText",
        "<p>To do so, this device acts as an USB ethernet device, and you can connect using the SSH protocol. \n"
        "On a non-demo device the username, password and IP addresses can be found here.</p>",
        "",
        False,
    ),
    ("SearchView", "File size", "", False),
    ("SearchView", "Last opened", "", False),
    ("SettingsWindow", "Marker", "", False),
    ("SetupPage", "%1Verify%2 passcode", "words between %1 and %2 will be highlighted", False),
    ("SetupPage", "Enter %1passcode%2", "words between %1 and %2 will be highlighted", False),
    ("SetupPageBase", "Latest software %1installed%2", "words between %1 and %2 will be highlighted", False),
    ("SetupPageBase", "Something %1went wrong%2", "words between %1 and %2 will be highlighted", False),
    ("StringUtils", "%1 %2", "", False),
    ("StringUtils", "Bytes", "", False),
    ("StringUtils", "GB", "", False),
    ("StringUtils", "KB", "", False),
    ("StringUtils", "MB", "", False),
    ("TagUtils", "You can sort your pages after combination of tags. \nSelect one that matches what you're looking for.", "", False),
    ("TagUtils", "You can tag pages, files, and folders to find them faster. All your tagged content will appear here so you can easily sort and filter notes and documents.", "", False),
    ("TemplateSelectorWindow", "Back", "Template selector", False),
}
DYNAMIC_SUPPLEMENT_KEYS = {
    ("SettingsModel", "Developer", "", False),
    ("SettingsModel", "Experimental", "", False),
    ("SettingsModel", "Wifi", "", False),
    ("xofm::libs::toolbar::PenColorModel", "Magenta", "", False),
}
MANUAL_TRANSLATIONS = {
    ("Experimental", "Add the option to search by Relevance"): "添加按相关性搜索的选项",
    ("Experimental", "NetworkManager wifi backend"): "NetworkManager Wi-Fi 后端",
    (
        "Experimental",
        "Switches out the cslwifi backend with NetworkManager, requires reboot after toggling",
    ): "将 cslwifi 后端替换为 NetworkManager；切换后需要重启",
    ("ExportIntegrationOptionsButton", "Export to storage integrations"): "导出至云存储服务",
    ("QObject", "Connection timed out"): "连接超时",
    ("QObject", "Connection to %1 failed"): "连接 %1 失败",
    ("QObject", "Download retail library"): "下载零售演示内容库",
    ("RetailSetupInfo", "Enter retail"): "进入零售演示模式",
    ("RetailSetupLanguage", "Welcome to retail"): "欢迎使用零售演示模式",
    ("ScreenShare", "Canceled by app"): "已由应用取消",
    ("retail::Downloader", "%1 is up to date, no download needed."): "%1 已是最新，无需下载。",
    ("retail::Downloader", "Could not write to %1"): "无法写入 %1",
    ("retail::Downloader", "Download failed with code: %1 %2"): "下载失败，错误代码：%1 %2",
    ("retail::Downloader", "Download failed with unexpected error."): "下载失败，发生意外错误。",
    ("retail::Downloader", "Downloading '%1' to '%2'"): "正在将“%1”下载到“%2”",
    (
        "rmsync::BuildServerTreeAction",
        "Failed building server state, you might need to update.",
    ): "无法构建服务器状态，您可能需要更新系统。",
    ("rmsync::BuildServerTreeAction", "Failed building server state."): "无法构建服务器状态。",
    ("rmsync::BuildServerTreeAction", "Server index is empty"): "服务器索引为空",
    ("rmsync::InvalidAction", "rmsync aborted to release entry locks"): "rmsync 已中止，以释放条目锁",
    (
        "rmsync::LoadPreviousTreeAction",
        "rmsync aborted due to missing user id",
    ): "缺少用户 ID，rmsync 已中止",
    (
        "rmsync::RequestMissingHashesAction",
        "rmsync aborted to release entry locks",
    ): "rmsync 已中止，以释放条目锁",
    ("rmsync::Synchronizer", "Unable to sync. Free up disk space."): "无法同步。请释放磁盘空间。",
    ("rmsync::TransfersAndRemovalCommon", "Failed syncing file system"): "文件系统同步失败",
    (
        "rmsync::TransfersAndRemovalCommon",
        "Unable to store downloaded file",
    ): "无法保存下载的文件",
    (
        "rmsync::UpdateServerRootHashAction",
        "rmsync aborted to release archived entry locks",
    ): "rmsync 已中止，以释放已归档条目锁",
    (
        "rmsync::UpdateServerRootSchemaAction",
        "rmsync aborted to release archived entry locks",
    ): "rmsync 已中止，以释放已归档条目锁",
    (
        "xofm::modules::wificore::cslbackend::CslWifiManager",
        "Connection to %1 failed",
    ): "连接 %1 失败",
    (
        "xofm::modules::wificore::cslbackend::CslWifiManager",
        "Incorrect EAP method input",
    ): "EAP 方法输入不正确",
    (
        "xofm::modules::wificore::cslbackend::CslWifiManager",
        "Incorrect Phase 2 method input",
    ): "第二阶段方法输入不正确",
    ("xofm::modules::wificore::cslbackend::CslWifiManager", "Password too long"): "密码过长",
    ("xofm::modules::wificore::cslbackend::CslWifiManager", "Password too short"): "密码过短",
    ("xofm::modules::wificore::cslbackend::CslWifiManager", "SSID too long"): "SSID 过长",
    ("xofm::modules::wificore::cslbackend::CslWifiManager", "SSID too short"): "SSID 过短",
}
QM_MAGIC = bytes.fromhex("3cb86418caef9c95cd211cbf60a1bddd")


def element_text(element):
    return "" if element is None else "".join(element.itertext())


def edge_whitespace(text):
    leading = re.match(r"\s*", text).group()
    trailing = re.search(r"\s*$", text).group()
    return leading, trailing


def key_digest(keys):
    payload = json.dumps(
        sorted(keys), ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class RmkitCnTranslationCatalogTests(unittest.TestCase):
    def test_chinese_catalog_is_complete_and_well_formed(self):
        catalog_path = (
            Path(__file__).resolve().parents[1]
            / "translations"
            / "reMarkable_zh_CN.ts"
        )
        root = ET.parse(catalog_path).getroot()
        context_names = set()
        keys = []
        catalog_entries = {}
        forbidden_entries = []
        empty_entries = []
        corrupt_entries = []
        placeholder_mismatches = []
        markup_mismatches = []

        for context in root.findall("context"):
            context_name = element_text(context.find("name"))
            context_names.add(context_name)
            for message in context.findall("message"):
                source = element_text(message.find("source"))
                comment = element_text(message.find("comment"))
                numerus = message.get("numerus") == "yes"
                keys.append((context_name, source, comment, numerus))

                translation = message.find("translation")
                if translation is None:
                    empty_entries.append((context_name, source))
                    continue
                if translation.get("type") in FORBIDDEN_TYPES:
                    forbidden_entries.append((context_name, source))

                forms = (
                    translation.findall("numerusform")
                    if numerus
                    else [translation]
                )
                if not forms:
                    empty_entries.append((context_name, source))
                    continue

                source_placeholders = Counter(PLACEHOLDER_RE.findall(source))
                source_tags = Counter(TAG_RE.findall(source))
                source_urls = Counter(URL_RE.findall(source))
                source_edges = edge_whitespace(source)
                catalog_entries[keys[-1]] = tuple(element_text(form) for form in forms)
                for form in forms:
                    if form.get("type") in FORBIDDEN_TYPES:
                        forbidden_entries.append((context_name, source))
                    translated = element_text(form)
                    entry = (context_name, source, translated)
                    if not translated.strip():
                        empty_entries.append(entry)
                    if SUSPICIOUS_REPLACEMENT_RE.search(translated):
                        corrupt_entries.append(entry)
                    if (
                        translated.count("\n") != source.count("\n")
                        or edge_whitespace(translated) != source_edges
                    ):
                        empty_entries.append(entry)
                    if Counter(PLACEHOLDER_RE.findall(translated)) != source_placeholders:
                        placeholder_mismatches.append(entry)
                    if Counter(TAG_RE.findall(translated)) != source_tags:
                        markup_mismatches.append(entry)
                    if not source_urls <= Counter(URL_RE.findall(translated)):
                        markup_mismatches.append(entry)

        self.assertEqual(len(keys), EXPECTED_MESSAGES)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(context_names), MINIMUM_CONTEXTS)
        self.assertFalse(forbidden_entries)
        self.assertFalse(empty_entries)
        self.assertFalse(corrupt_entries)
        self.assertFalse(placeholder_mismatches)
        self.assertFalse(markup_mismatches)

        supplement_keys = STATIC_SUPPLEMENT_KEYS | DYNAMIC_SUPPLEMENT_KEYS
        self.assertEqual(len(STATIC_SUPPLEMENT_KEYS), 64)
        self.assertEqual(
            key_digest(STATIC_SUPPLEMENT_KEYS),
            STATIC_SUPPLEMENT_KEY_SHA256,
        )
        self.assertEqual(len(DYNAMIC_SUPPLEMENT_KEYS), 4)
        self.assertTrue(supplement_keys <= set(keys))
        stock_keys = set(keys) - supplement_keys
        self.assertEqual(len(stock_keys), STOCK_MESSAGES)
        self.assertEqual(key_digest(stock_keys), STOCK_KEY_SHA256)

        self.assertEqual(len(MANUAL_TRANSLATIONS), 33)
        for (context, source), expected in MANUAL_TRANSLATIONS.items():
            self.assertEqual(
                catalog_entries[(context, source, "", False)],
                (expected,),
            )

        for source, expected in SETTINGS_MODEL_TRANSLATIONS.items():
            self.assertEqual(
                catalog_entries[("SettingsModel", source, "", False)],
                (expected,),
            )
        for source, expected in PEN_COLOR_TRANSLATIONS.items():
            self.assertEqual(
                catalog_entries[
                    ("xofm::libs::toolbar::PenColorModel", source, "", False)
                ],
                (expected,),
            )
        for context in READING_LIGHT_CONTEXTS:
            self.assertEqual(
                catalog_entries[(context, READING_LIGHT_DESCRIPTION, "", False)],
                ("从右上角向下滑动打开快捷设置，即可调整亮度。",),
            )

    def test_compiled_qm_matches_the_catalog_and_runtime_hash(self):
        qm_path = (
            Path(__file__).resolve().parents[1]
            / "translations"
            / "reMarkable_zh_CN.qm"
        )
        data = qm_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            _rmkit_cn.LOCALIZED_QM_SHA256,
        )
        self.assertEqual(data[: len(QM_MAGIC)], QM_MAGIC)

        sections = {}
        offset = len(QM_MAGIC)
        while offset < len(data):
            self.assertLessEqual(offset + 5, len(data))
            tag = data[offset]
            length = int.from_bytes(data[offset + 1 : offset + 5], "big")
            offset += 5
            self.assertLessEqual(offset + length, len(data))
            sections[tag] = data[offset : offset + length]
            offset += length

        self.assertEqual(offset, len(data))
        self.assertIn(0x42, sections)
        self.assertIn(0x69, sections)
        self.assertEqual(len(sections[0x42]), EXPECTED_MESSAGES * 8)
        message_offsets = {
            int.from_bytes(sections[0x42][i + 4 : i + 8], "big")
            for i in range(0, len(sections[0x42]), 8)
        }
        self.assertEqual(len(message_offsets), EXPECTED_MESSAGES)
        self.assertTrue(all(offset < len(sections[0x69]) for offset in message_offsets))


if __name__ == "__main__":
    unittest.main()
