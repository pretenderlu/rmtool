# Complete Chinese Catalog Rebuild

## Goal

Rebuild the xochitl Chinese translation catalog for reMarkable Paper Pro
firmware `20260629074044` so it covers every active message present in the
current English, French, German, or Spanish device catalogs, plus proven
translation lookups in the QML embedded in that exact xochitl binary.

This work changes translation assets only. The existing firmware gate, French
carrier slot, backup/restore behavior, and stop-before-write deployment order
remain unchanged.

## Root Cause

The current Chinese catalog was rebased from `reMarkable_en.qm`. English is
xochitl's source language, so its QM catalog contains only 873 messages and is
not a complete inventory of translatable UI strings. The three non-English
catalogs contain roughly 1650 messages each. Their union with English contains
1712 active message keys, leaving the current Chinese catalog short by 839.

Visual review exposed a second gap: the stock catalogs lag the QML embedded in
the supported xochitl build. A read-only audit of the firmware binary found 97
Qt resource bundles, mapped 638 QML files, and inspected 1568 `qsTr` or
`qsTranslate` calls. It found 140 exact static keys absent from the 1712-key
stock union. The settings sidebar also translates a runtime enum through
`qsTranslate("SettingsModel", model.title)`, requiring nine explicit title
keys. This explains both the untranslated settings categories and the new
reading-light description.

The pen color menu similarly translates a runtime value through
`xofm::libs::toolbar::PenColorModel`. The current firmware exposes `Magenta`,
which is absent from the stock catalogs and requires one explicit key.

## Catalog Baseline

The baseline is the union of the four stock catalogs from the supported
firmware:

- `reMarkable_en.qm`
- `reMarkable_fr.qm`
- `reMarkable_de.qm`
- `reMarkable_es.qm`

A message key consists of context, source text, disambiguation comment, and
numerus flag. Active messages emitted by `lconvert` from the stock QM files
form the stock layer. The firmware layer adds the 140 static QML keys proven by
the binary audit, nine finite `SettingsModel` enum values, and `Magenta` from
the finite pen color model. The final catalog contains exactly 1862 unique
keys.

## Merge Order

For every baseline key, choose the Chinese translation in this order:

1. Current rmtool Chinese catalog: 873 exact-key matches.
2. Previous rmkit Chinese catalog: 164 additional exact-key matches.
3. New Simplified Chinese translation: approximately 675 stock-union keys.
4. Firmware QML supplement: 140 exact static keys plus ten finite dynamic
   values, reusing an existing context/source translation where only the
   disambiguation comment changed.

Foreign-language target text is never copied. Source text, comments, numerus
metadata, and meaningful whitespace come from the stock baseline.

## Translation Rules

- Use concise Simplified Chinese suitable for an e-ink tablet UI.
- Preserve all Qt placeholders exactly, including order-sensitive `%1`, `%2`,
  `%n`, and `%L1` forms.
- Preserve HTML/XML tags, entities, URLs, line breaks, and intentional leading
  or trailing whitespace.
- Translate duplicate English source strings according to their context.
- Keep product names and file formats unchanged where appropriate.
- Provide every required numerus form and leave no empty or unfinished target.

## French Carrier Display Alias

The localization continues to use xochitl's `fr_FR` language code and
`reMarkable_fr.qm` carrier file. In the `LanguageAndKeyboard` translation
context, the source label `French` is displayed as `简体中文` instead of `法语`.
This is a cosmetic alias only; it does not add a `zh_CN` language code or
change the underlying French slot.

The same context/source key can also be used by keyboard or handwriting
language selectors. Those occurrences may therefore display `简体中文` while
retaining their original French behavior. This known trade-off is accepted in
order to keep the implementation translation-only and avoid QMD, xovi, and
systemd changes.

## Validation

Automated checks must fail on the current 873-message catalog and pass only
when the rebuilt catalog satisfies all of these conditions:

- Exactly 1862 unique message keys and at least 300 contexts.
- No empty, unfinished, obsolete, or vanished translations.
- Source and translation placeholder multisets match for every message.
- Source and translation markup tag multisets match for every message.
- No duplicate message keys.
- Strict Qt 6 `lrelease -fail-on-unfinished -fail-on-invalid` succeeds.
- A QM-to-TS round trip retains all 1862 messages.
- Re-running the task-local firmware QML audit reports zero missing static
  keys and zero unmapped QML resource bundles.
- Regression checks require all nine `SettingsModel` titles and the exact
  reading-light description key in both `DisplaySettingsHeader` and
  `KeyboardSettingsHeader`, plus `PenColorModel / Magenta` and
  `LanguageAndKeyboard / French = 简体中文`.
- The full rmtool test suite remains green.

After local validation, deploy the new QM through the existing safe backend,
close the deployment SSH session, restart separately, and verify xochitl stays
active with `NRestarts=0`. Visual review should cover settings, library,
document actions, search, onboarding, account, and error dialogs.

## Out of Scope

- No new device features, controls, services, QMD patches, IME, or xovi code.
- No support expansion to other firmware builds.
- No change to the French carrier mechanism or device backup paths.
- No runtime translation dependency or network translation service.
- No claim that arbitrary, unbounded dynamic `qsTr(variable)` calls can be
  enumerated from QML alone; only the finite, proven settings and pen-color
  values are added by this supplement.
