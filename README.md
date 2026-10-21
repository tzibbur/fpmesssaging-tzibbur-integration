# Tzibbur integration for FP Messaging — kit 2

This package carries the Tzibbur integration in a primary replacement DEX.
It also contains the resource additions, exact
manifest additions, editable smali, a Python installer and focused installer tests.

Resources and smali are stored together in `payload.zip`. Leave that file zipped
when uploading this kit to a repository. The installer verifies and extracts it
into temporary storage automatically, then removes that storage on completion
or failure. The kit folder contains fewer than 100 files, including tool notices.

## Quick start

Install Python 3.10 or newer and Java 17.
[APKEditor 1.4.5](https://github.com/REAndroid/APKEditor/releases/tag/V1.4.5)
is included in `tools/` and selected automatically. Its SHA-256 is
`71999a1f28cf6b457aff17c139436349cd6ea30d75a0f9cd52f07bd52e21897b`.
No Python packages are needed. Python, Java, Android SDK signing tools and
signing keys are not bundled. Tool attribution and licenses are in `tools/notices/`.

Unzip this package, open a terminal in its folder, and run:

```sh
python integrate.py "Messages-original.apk" --check
python integrate.py "Messages-original.apk" --out "result"
```

Use `python3` instead of `python` where appropriate. Paths may contain spaces.
`--apkeditor` or the `APKEDITOR` environment variable can override the bundled
tool with a JAR path or executable on PATH. `--java` selects a Java
executable. The script does not download anything, modify the input APK or install
on a device. The output directory must be new. An incompatible target produces
an error before output is published; there is no bypass switch.

Output:

| File | Purpose |
| --- | --- |
| `Messages-tzibbur-unsigned.apk` | Complete integrated APK, ready to align and sign |
| `classes.dex` | Replacement DEX generated for this exact target's resource IDs |
| `AndroidManifest.xml` | Complete merged manifest, for review |
| `resource-map.json` | Exact source-to-target resource ID mapping by type/name |
| `report.json` | Input/output hashes, version, class/resource counts and checks |
| `build.log` | APKEditor output |

The default versionCode is the input's versionCode + 1; the versionName gets
`-tzibbur18` appended. If the phone already has a higher version installed, use
`--version-code NUMBER` with a number higher than that installed version.

## Align, sign and install

Use the receiving app's existing signing key. An updated system Messages APK
must retain the appropriate signing identity and privileges. The kit contains
no signing keys, certificates or complete app APKs.

With current Android SDK Build Tools on PATH:

```sh
zipalign -P 16 -f 4 result/Messages-tzibbur-unsigned.apk result/Messages-tzibbur-aligned.apk
apksigner sign --ks company-messaging.jks --out result/Messages-tzibbur.apk result/Messages-tzibbur-aligned.apk
apksigner verify --verbose result/Messages-tzibbur.apk
adb.exe install -r result/Messages-tzibbur.apk
```

On Windows use `zipalign.exe` / `apksigner.bat` as appropriate. Under WSL,
Windows `adb.exe` needs a Windows APK path such as `C:\adb\Messages-tzibbur.apk`.
Align before signing; any later APK modification invalidates the signature.
See [zipalign](https://developer.android.com/tools/zipalign) and
[apksigner](https://developer.android.com/tools/apksigner).

## What goes in the first DEX

There are **425 classes**: 418 new Tzibbur/bridge classes and these seven complete
replacement Messages classes, with the integration hooks already present:

```text
com.android.mms.MmsApp
com.android.mms.ui.ComposeMessageActivity
com.android.mms.ui.MessagingPreferenceActivity
com.android.mms.ui.ConversationList
com.android.mms.ui.ConversationList$3
com.android.mms.ui.ConversationList$12
com.android.mms.transaction.MessagingNotification
```

The resulting APK contains:

```text
classes.dex    = our replacement classes and Tzibbur code
classes2.dex   = the target's original classes.dex, unchanged
classes3.dex   = the target's original classes2.dex, unchanged (if present)
...
```

All original DEX bytes are verified after packing. Whole-class replacements take
precedence in the standard Android app class loader. The installer checks the
seven original class bodies before allowing replacement. Differences in resource
IDs, resource comments and debug line numbers are normalized. Existing changes
inside those seven classes require a new compatible package; the script will
identify the class that needs adapting. Related classes remain in the original
DEX, and required inheritance/method/field contracts are checked.

`classes.dex` at the package root is a prebuilt reference using the source
resource IDs. Use the DEX generated in `result` for the receiving APK. It can have
different numeric IDs even though it implements the same behavior.

## Resources and manifest: exact changes

`manifest-additions.xml` contains the complete permission/component additions.
Merge its `uses-permission` children into the manifest root (deduplicated by name)
and its `application` children into the existing application. Keep the existing
application class, package, labels, unrelated components and permissions. Each
Tzibbur screen declares its own theme. The secret-code receiver is included.

The `res/` folder inside `payload.zip` contains the resource files and value
entries to add. The `smali/` folder holds all editable replacement classes.
The resource additions contain
**473 new resource names**, listed in `bundle.json` under `added_resources`.
The one existing layout changed is `layout/conversation_list_item.xml`, which
adds the paused-notifications bell. The installer checks its original structure.
Existing host colors, strings, styles and IDs are retained. New Tzibbur resources
refer to the target's host styles where appropriate, so its color choices remain.

The Python script first verifies `payload.zip`, checks its entries against the
hashes in `bundle.json` under `payload_files`, and extracts it into temporary
storage. It rejects unexpected, duplicate or unsafe paths. It then:

1. Decodes the target with APKEditor and checks the compatibility contract.
2. Matches borrowed host resources by type/name.
3. Allocates unused IDs for new names without renumbering existing resources.
4. Merges value entries, configuration variants, layouts and drawables.
5. Relinks resource operands throughout **all replacement classes**, including
   host classes. It handles static R fields, literal constants, `const/high16`,
   integer arrays and reordered/noncontiguous switch keys. Strings and ordinary
   non-resource constants such as `Integer.MAX_VALUE` remain unchanged.
6. Adds the manifest entries and builds the replacement DEX/resource table.
7. Appends the target's original DEX files at the next positions and verifies
   their byte-for-byte preservation.
8. Decodes the built APK again and verifies every replacement class, the compiled
   resource mapping and the added manifest components before publishing output.
9. Removes old APK signatures and stale baseline DEX profiles. Output must be
   aligned and signed by the receiving company.

`resource-map.json` in the result is the exact numeric mapping for a particular
APK. Do not copy the source APK's entire `resources.arsc` over another app's table.
For manual assembly, use the generated manifest/resources and the **target-specific**
DEX together. The Python flow is the supported way to perform these steps.

## Supported base and compatibility limits

This kit targets the `com.android.mms` TCL/CAF app, Android 11, targetSdk 30.
Provide your own original Messages APK. The receiving app's unrelated code and
resource values are retained. No reference APK, original APK download URL,
original APK hash or signing certificate is distributed with this kit.

Other APKs may pass when their seven replaced classes, required dependency
contracts, resource names and inbox layout are compatible. A resource ID or color
change alone does not require another kit. Changed class logic/layout structure
may require rebasing. Passing structural checks does not replace keypad, UI,
delivery and notification checks on the company's firmware. TCL's vendor menu
classes and the existing app's system privileges are still required. This is
not a claim of support for unrelated Android messaging apps or newer SDK targets.

## Included behavior

- Mixed SMS/MMS and Tzibbur inbox, logo and correct initial selection.
- Native styled Tzibbur conversations/settings, numbered settings, public live
  privacy/terms screens, solid dialog backgrounds and login Back navigation.
- D-pad selection and Copy/Delete in Tzibbur threads (local deletion).
- Paste in SMS/MMS/new drafts and Tzibbur; per-thread notification pause for both.
- Tzibbur account, devices, invitations, blocks, connection and notification settings.
- The exact current gate: `not_tag=1` always blocks Tzibbur. Otherwise,
  `tzibbur.allow=1` enables it and clears the guest
  `tzb_config/disable_tzibbur` preference. A disabled result sets that preference.

## Checks and manual acceptance

Run the installer tests with:

```sh
python -m unittest test_integrate -v
```

See `VALIDATION.md` for the actual validation performed on this kit. The 44 app
tests passed for the integrated feature build; this package retains that code.

After signing/installing, check the mixed inbox, New, SMS and Tzibbur conversations,
Paste, message Copy/Delete, settings/Back/legal pages, and notification pause.
Check both enable paths and confirm `not_tag=1` wins. Test SMS/MMS and Tzibbur
delivery using the intended firmware/accounts. The installer itself never sends
messages, changes device flags, accesses an account or connects to the Tzibbur API.

Keep the original APK and its signing setup for rollback. No user database,
account token, signing key, device identifier or workstation backup is in this kit.
