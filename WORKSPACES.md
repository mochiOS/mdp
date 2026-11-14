# Independent producers and SDK contexts

MDP is a multi-project development tool, not an OS build system. A workspace
registers repositories; each repository owns its build entry, recipes and output
placements. Product-specific compiler options belong in that product's SDK, not
in MDP. Composition never invokes a producer.

```sh
mdp workspace lock
mdp workspace build -j 4
```

`mdp.workspace.toml` uses format 1, `target`, `abi`, explicit `inputs`, and repeated
`[[projects]]` with unique `name`, repository-relative `path` and optional `manifest`.
`source_only = true` registers source dependencies without inventing an executable.
`optional = true` declares explicit post-processing/host tools rather than image
prerequisites. `--select NAME` builds that producer and its prerequisites; the
resulting composition lock includes previously successful artifacts only while their inputs still match.

A project `mdp.toml` owns `[build].command` (an argv array), `requires` (producer
IDs), `sources` (additional pinned source IDs), `inputs` (existing workspace-relative files consumed by this producer), optional `artifact` (relative to
that repository), and `outputs` (workspace-relative generated cache prerequisites).
The scheduler rejects cycles and missing producers, locks the workspace against
concurrent builds, propagates real command failures, and verifies each exported
archive. Cache receipts require matching source, SDK/configuration, dependency
and archive identities. An unchanged artifact can be reused; missing or corrupt
archives are rebuilt. No successful receipt is fabricated for a failed command.

`mdp.workspace.lock` records exact Git revisions, local source edits and declared
configuration/SDK input hashes. `[[external_inputs]]` pins an explicitly imported
SDK tree by content, without recording its machine's absolute path. Source edits
require an explicit `workspace lock`. Cargo's own lock resolutions are recorded
in build receipts and are never restored, reordered back or overwritten with an
old copy. Local development builds can update their Cargo locks; commit those
updates. Raw lock digests remain recorded; build caching ignores Cargo unused-patch diagnostics, without editing or restoring those locks. Additional source repositories do not consume their unrelated workspace lockfiles. Non-compiling context producers can set `resolution = false`. This is not a hermetic build sandbox or an automatic release attestation.

Source identity measures the actual tracked and untracked working content using Git-compatible blob identities. Committing the same reviewed content does not invalidate it; changing source bytes does. Cargo/MDP lock entries are measured as dependency resolution instead. Exact Git revisions, dirty-state diagnostics and raw lock hashes remain in receipts.

The workspace's `[environment]` contains explicit strings, with `{workspace}`
expanded to the local checkout root. `MDP_JOBS`, `MDP_PROJECT`, `MDP_WORKSPACE`
and `MDP_REQUIRES` describe the current invocation. Product-specific SDK code can
export an SDK context. Setting `[build].sdk_context = true` on a consumer and the
workspace's `sdk_context = "relative/context.json"` records its actual built SDK
identity in that consumer's `mdp.lock` before compilation. This resolves a pinned
source SDK into exact artifact hashes; it does not select a moving SDK version.

## A repository outside the workspace

An independently checked-out consumer can use an explicitly selected local SDK:

```sh
mdp lock --context /path/to/developer-context.json
mdp build --context /path/to/developer-context.json
```

The format-1 descriptor contains `sdk` (including a SHA-256 `id`), string
`environment` bindings, and measured `artifacts` and `sources`. MDP validates
those identities before trusting the SDK environment. A changed context requires
reviewing and updating `mdp.lock`. Context descriptors are local SDK setup data;
their absolute host paths are not suitable for publishing in an MDP or release.
The product's SDK setup owns this descriptor, sources, compilers and headers.
A source SDK may include pinned source dependencies; independence means no call
to the product root's build graph, not rebuilding or bundling a compiler per app.

## Trusted pipeline helper

`mdp pipeline --recipe mdp.recipe.json` executes a repository-owned format-1
recipe with `steps` (explicit Bash commands), optional string `bindings`, and
explicit `exports`. Only a reviewed local recipe is executed: an archive manifest
can never contain executable hooks.

Bindings use `$(UPPERCASE_NAME)`. Ordinary shell substitutions such as
`$(date +%s)` are preserved. `MDP_VARIABLES` selects an SDK binding JSON;
`MDP_VARIABLE_OVERLAYS` optionally contains a JSON list of locally generated
binding files. The repository's bindings override SDK defaults and `PROJECT`
always denotes that repository. Each export declares `source`, `area`,
`destination`, optional mode, `when` binding conditions, `exclude` patterns and
`preserve_links`. Directory exports are expanded into explicit measured files.
Generated pack specifications and dependency mirrors stay under `out/mdp/`.
SDK dependencies are recorded from successful producer receipts. Producers must
not mutate their export staging after publication; archives are independent,
measured byte snapshots, not writable links back into a producer's cache.

## File aliases (MDP format 2)

Ordinary archives remain format 1. A producer requiring file aliases uses
`[[links]]` in its pack spec, and the archive manifest becomes format 2. Each
link declares `area`, `destination`, and a relative `target`. Its final target
must be another explicitly declared file in the same area. Escapes, dangling
links, cycles and paths beneath links are rejected. Tar payloads still contain
regular files only; verified aliases are created after those files. The initial
format-2 implementation does not allow directory aliases.

`mdp assemble --area system --area boot ...` verifies every artifact and payload
but materializes only requested areas. A product can thus verify SDK provenance
without copying SDKs into its image. The receipt records this area selection.
