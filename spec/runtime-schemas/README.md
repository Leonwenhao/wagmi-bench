# Runtime schemas

This directory contains input schemas owned by the executable runtime. They are
not part of the frozen M0 interchange-contract set in `spec/schemas/`.

`episode_config.v1.schema.json` describes the operator-authored configuration
accepted before it is normalized to the frozen
`bundle_manifest.run_config` shape. The frozen bundle schema remains the
replay-facing source of truth.
