# Complete Chinese Catalog Rebuild

## Goal

Rebuild the xochitl Chinese translation catalog for reMarkable Paper Pro
firmware `20260629074044` so it covers every active message present in the
current English, French, German, or Spanish device catalogs.

This work changes translation assets only. The existing firmware gate, French
carrier slot, backup/restore behavior, and stop-before-write deployment order
remain unchanged.

## Root Cause

The current Chinese catalog was rebased from `reMarkable_en.qm`. English is
xochitl's source language, so its QM catalog contains only 873 messages and is
not a complete inventory of translatable UI strings. The three non-English
catalogs contain roughly 1650 messages each. Their union with English contains
1712 active message keys, leaving the current Chinese catalog short by 839.

## Catalog Baseline

The baseline is the union of the four stock catalogs from the supported
firmware:

- `reMarkable_en.qm`
- `reMarkable_fr.qm`
- `reMarkable_de.qm`
- `reMarkable_es.qm`

A message key consists of context, source text, disambiguation comment, and
numerus flag. Only active messages emitted by `lconvert` from the stock QM
files are included. The expected union contains exactly 1712 unique keys.

## Merge Order

For every baseline key, choose the Chinese translation in this order:

1. Current rmtool Chinese catalog: 873 exact-key matches.
2. Previous rmkit Chinese catalog: 164 additional exact-key matches.
3. New Simplified Chinese translation: approximately 675 remaining keys.

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

## Validation

Automated checks must fail on the current 873-message catalog and pass only
when the rebuilt catalog satisfies all of these conditions:

- Exactly 1712 unique message keys and at least 300 contexts.
- No empty, unfinished, obsolete, or vanished translations.
- Source and translation placeholder multisets match for every message.
- Source and translation markup tag multisets match for every message.
- No duplicate message keys.
- Strict Qt 6 `lrelease -fail-on-unfinished -fail-on-invalid` succeeds.
- A QM-to-TS round trip retains all 1712 messages.
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
