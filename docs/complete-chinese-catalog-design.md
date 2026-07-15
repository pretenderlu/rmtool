# Production Chinese Catalog Baseline

## Goal

Build the xochitl Chinese translation catalog for reMarkable Paper Pro
production firmware `3.27.3.0` (internal version `20260612085811`) so it
covers every active message present in the English, French, German, or Spanish
device catalogs, plus proven translation lookups in that exact xochitl binary.

Deployment remains translation-only and keeps the French carrier slot,
backup/restore transaction, and stop-before-write order. The firmware gate and
carrier hashes are updated to the production baseline.

## Root Cause

English is xochitl's source language, so its production QM catalog contains
only 918 messages and is not a complete inventory. The exact union of all four
stock catalogs contains 1779 active message keys. The beta-targeted Chinese TS
matches 1675 exactly and misses 104 production keys.

The stock catalogs also omit live QML lookups. A read-only audit found 91 Qt
resource bundles, mapped 620 QML files, and inspected 1507 `qsTr` or
`qsTranslate` calls. It found 64 exact static keys absent from the 1779-key
stock union. The settings sidebar additionally translates finite runtime enum
values through `qsTranslate("SettingsModel", model.title)`.

The finite runtime supplement contains `SettingsModel / Wifi`,
`SettingsModel / Developer`, `SettingsModel / Experimental`, and
`xofm::libs::toolbar::PenColorModel / Magenta`.

## Catalog Baseline

The baseline is the union of the four stock catalogs from the supported
firmware:

- `reMarkable_en.qm`
- `reMarkable_fr.qm`
- `reMarkable_de.qm`
- `reMarkable_es.qm`

A message key consists of context, source text, disambiguation comment, and
numerus flag. Active messages emitted by `lconvert` from the stock QM files
form the stock layer. The firmware layer adds the 64 static QML keys proven by
the binary audit and four finite runtime keys. The final catalog contains
exactly 1847 unique keys.

## Merge Order

For every baseline key, choose the Chinese translation in this order:

1. Current rmtool Chinese catalog: 1675 exact production-key matches.
2. Previous rmkit Chinese catalog: 47 additional exact-key matches.
3. Current Chinese catalog after same-context entity normalization: 16 keys.
4. Exact source translation reused from another current context: eight keys.
5. New Simplified Chinese translation: 33 keys.
6. Firmware supplement: 64 static QML keys plus four finite runtime values.

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

Automated checks pass only when the production catalog satisfies all of these
conditions:

- Exactly 1847 unique message keys and at least 300 contexts.
- No empty, unfinished, obsolete, or vanished translations.
- Source and translation placeholder multisets match for every message.
- Source and translation markup tag multisets match for every message.
- No duplicate message keys.
- Qt 6 `lrelease -fail-on-unfinished` succeeds.
- A QM-to-TS round trip retains all 1847 messages.
- Re-running the task-local firmware QML audit reports zero missing static
  keys and zero unmapped QML resource bundles.
- Regression checks pin the exact 64-key static supplement by its canonical
  key-set digest and all 33 manual translation targets, reject suspicious
  replacement-character runs, and require the four finite runtime keys and
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
