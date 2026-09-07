# Personal AOSP research

- Read README.md, PLAN.md and targets/aosp13.json before work.
- This repository owns the product, research-info and maintenance scripts. AOSP core edits belong to the precise child Git repository in the configured experimental tree.
- Never modify the configured reference AOSP tree, its existing changes, or its out directory. Do not repair the existing Contacts/libcore differences.
- Inspect Git status before changing branches, synchronizing or building. Stop on conflicts or unsaved edits; do not use reset --hard, clean -fd or force checkout.
- Outside initial setup, synchronize only device/kyler/research. Use ordinary Git for core branches, and save the branch before returning to its baseline.
- Build only in the experimental tree's out directory. Use the emulator serial recorded by lab; never act on an unspecified device.
- Every meaningful change requires appropriate tests. Do not equate module compilation with a full image boot or assume Android 14/15 compatibility.
- Keep machine-specific absolute paths in .lab.local.json (ignored). Keep artifacts, emulator data and Git backups in the configured state directory.
- Do not create additional agents or team infrastructure. Keep this a small personal project.
