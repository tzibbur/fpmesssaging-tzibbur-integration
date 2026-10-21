# Validation — 2026-10-21

## Compatible host validation

- Validated with a compatible Android 11 TCL/CAF Messages host.
- All seven replaced Messages classes matched the supported compatibility checks.
- The receiving app's different host colors and unrelated code were retained.
- Built a primary DEX with 425 classes; the original DEX was retained byte-for-byte
  as `classes2.dex`.
- Added 473 resources. Every original target resource ID and original value
  entry was checked against the rebuilt APK and remained unchanged.
- Decoded the resulting APK and compared all 425 replacement classes after
  resource relinking and normalization of non-semantic disassembly differences.
  All matched. Added manifest components and the resource mapping also matched.

## Resource portability stress case

- Created a separate test fixture with non-attribute resource
  IDs shifted and reversed. Attribute IDs were kept stable to preserve styleable
  indexing. The test APK's own code was relinked to those IDs before integration.
- The kit integrated successfully with **3,197** source-to-target IDs changing.
- The rebuilt primary DEX's 425 classes matched the expected relinked code.
- The manifest additions matched, and the target's existing IDs were all retained.
- This fixture is not a company release and was not installed on the phone.

## Installer tests

All **14** standard-library Python tests pass. They cover:

- ID allocation while retaining existing IDs, and collision rejection.
- Field/constants/array remapping and `const/high16` width changes.
- Reordered packed/sparse switch keys and prevention of double remapping.
- String literals, escaped quotes, comments and non-resource integer constants.
- Semantic fingerprint normalization for debug information, branch-label aliases,
  register declarations and method ordering; actual logic changes remain distinct.
- Multiple original DEX files retained at shifted filenames.
- Rejection of missing DEX positions and modified package payloads.
- Retention of original manifest attributes/components while adding ours.

The integrated feature build's 44 Robolectric tests passed before this export.
No app feature implementation was changed while preparing this kit.

## Device check

- Installation succeeded on an Android 11 TCL T408DL test device.
- The existing app data and device enable settings were retained.
- A small `app_process` probe opened the installed APK using Android's
  `PathClassLoader`. It resolved `ConversationList.tzibInboxChanged()`, which
  exists only in our primary replacement class, and the Tzibbur secret-code
  receiver. This confirms the duplicate-class precedence on this firmware.
  The temporary device probe file was removed afterward.
- UI, keypad, account actions and real message/notification delivery remain for
  the user's manual acceptance checks. No messages or authentication actions were
  sent during packaging verification.

The structural checks and device class-loading probe establish this package's
tested scope. A different company's firmware and materially changed host classes
still need compatibility review and manual acceptance.

## Distribution contents

The shareable kit includes only the integration DEX/smali, resource additions,
manifest additions, installer, tests, documentation and the bundled build tool.
Complete host APKs, device build reports, original APK links/hashes, signing
certificates and private signing material are excluded.
