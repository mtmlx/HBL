# Package path boundary — CodeQL alert 40

## Outcome: fixed in candidate source

The HTTP package routes accepted arbitrary output directories/filenames. Absolute
filenames and traversal could escape the output tree; registration subsequently
read the resulting path. Direct exfiltration of an unchanged file was not proven
because generation writes the target first. The missing filesystem boundary was
real and is now enforced before rendering or reading.

HTTP paths are confined to the configured runs directory after resolving symlinks.
Generated identifier components and requested filenames are checked. Registration
requires an explicit authorized root and checks containment before reading. It
validates, hashes, and uploads one byte snapshot. Trusted CLI callers retain their
explicitly selected output directory; Lambda callers retain their /tmp output
directories; the default HTTP per-task layout is preserved.

Changed boundary files within `src/mtm_hbl`: `safe_paths.py`, `api/main.py`,
`clickup_hbl_generator.py`, `verification/aws_repository.py`, `pdf/hbl_package.py`;
also `tools/issue_dev_hbl_package.py`.

Thirteen focused tests in `tests/test_package_path_boundary.py` pass. They reject
absolute paths, traversal, sibling-prefix and symlink escapes before rendering,
file reads, or AWS access; confirm nested paths; and preserve default task routing.
Existing renderer/registration/CLI-compatible root tests remain part of the full
suite. Independent read-only investigation and candidate review completed; the
reviewer's confirmed default-directory regression was corrected and tested.

Commands: `python -m pytest tests/test_package_path_boundary.py -q` passed;
`git diff --check` passed. Full suite and GitHub CodeQL are checked on the final
candidate commit before release. No live shipment or document was used for this fix.
