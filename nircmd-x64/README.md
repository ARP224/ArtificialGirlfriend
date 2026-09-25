# NirCmd (bundled third-party tool)

This folder contains **NirCmd x64** by Nir Sofer (NirSoft), unmodified:

- `nircmd.exe` / `nircmdc.exe` — the command-line tool itself
- `NirCmd.chm` — official help file (includes the license text)

Official site / latest version: https://www.nirsoft.net/utils/nircmd.html

NirCmd is used by AG's optional Command Execution feature (volume control,
monitor off, etc. — see `backend/tools/command_executor.py`). It is invoked
directly from this folder; no installation or PATH setup is required.

## License

NirCmd is freeware, © Nir Sofer. Quoted from the License section of
`NirCmd.chm`:

> This utility is released as freeware. You are allowed to freely distribute
> this utility via floppy disk, CD-ROM, Internet, or in any other way, as long
> as you don't charge anything for this. If you distribute this utility, you
> must include all files in the distribution package, without any
> modification!

AG is distributed free of charge and this folder contains the complete,
unmodified NirCmd package, satisfying both conditions.

This folder is **not** covered by AG's AGPL-3.0 license - see the SCOPE section
of the repository's `LICENSE`. If you redistribute AG for a fee, delete this
folder first: NirCmd's license forbids charging for it, and the AGPL neither
covers nor relicenses it. Only the optional Command Execution feature is
affected; AG reports the tool as missing and keeps running.

## Note on antivirus false positives

NirSoft system utilities are frequently flagged by antivirus software as
suspicious because they can control system settings. This is a well-known
false positive (see https://www.nirsoft.net/false_positive_report.html).
If your antivirus quarantines `nircmd.exe`, AG's Command Execution feature
will report the tool as missing; restore the file or re-download it from
the official site.
