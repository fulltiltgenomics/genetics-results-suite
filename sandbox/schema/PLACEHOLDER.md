# PLACEHOLDER — not the real schema documentation

This file exists only so the sandbox image has something to COPY into
`/genetics/schema/`. It is **not** generated content and carries no schema.

`genetics-results-suite-4h6.13` owns generation and must replace it.

## Contract for 4h6.13

- Write generated **schema markdown** into this directory, `sandbox/schema/`, one
  file per view (or whatever grouping 4h6.13 chooses — the image copies the whole
  directory and does not care about the file names).
- The image copies `sandbox/schema/` to **`/genetics/schema/`**, owned `65532:65532`,
  mode read-only (the root filesystem is read-only at runtime).
- The path is exported to the running container as `GENETICS_SCHEMA_DIR`. Read that
  variable rather than hardcoding the path.
- Delete this file in the same change that adds the generated output. The Dockerfile
  needs the directory to be non-empty, so do not leave it empty.
- Nothing in the image reads these files at build time; they are documentation the
  model-authored script (and the system prompt builder) reads at runtime. Adding
  files here does not require a Dockerfile change.
