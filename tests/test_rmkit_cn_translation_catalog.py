import re
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


EXPECTED_MESSAGES = 1862
MINIMUM_CONTEXTS = 300
FORBIDDEN_TYPES = {"unfinished", "obsolete", "vanished"}
PLACEHOLDER_RE = re.compile(r"%(?:L\d+|\d+|n)")
TAG_RE = re.compile(r"</?([A-Za-z][\w:.-]*)(?:\s[^<>]*?)?/?>")
URL_RE = re.compile(r"https?://[^\s<]+")
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
READING_LIGHT_CONTEXTS = (
    "DisplaySettingsHeader",
    "KeyboardSettingsHeader",
)
READING_LIGHT_DESCRIPTION = (
    "Manage the brightness level in Quick settings by swiping down in the "
    "top-&#8288;right&#xa0;corner."
)


def element_text(element):
    return "" if element is None else "".join(element.itertext())


def edge_whitespace(text):
    leading = re.match(r"\s*", text).group()
    trailing = re.search(r"\s*$", text).group()
    return leading, trailing


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
        self.assertFalse(placeholder_mismatches)
        self.assertFalse(markup_mismatches)

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


if __name__ == "__main__":
    unittest.main()
