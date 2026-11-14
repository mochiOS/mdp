# mdp

Multi-project development tool.

MDP separates project-owned builds from composition. A project builds itself and
exports a `.tar.mdp`; a composition consumes **locked artifacts only**. Assembly
never builds projects, downloads dependencies, runs package scripts, signs an
image, or deploys a product. MDP has no knowledge of mochiOS or disk partitions.

Requires Python 3.11 or newer. No third-party runtime dependencies are needed.
Run `./mdp --help` from a checkout, or install with `python3 -m pip install .`.

## Build a project

Create `mdp.toml` in the project repository:

```toml
format = 1

[package]
name = "example"
version = "1.0.0"
target = "x86_64-example"
abi = "example-1"

[build]
command = ["python3", "tools/build.py"]

[[files]]
source = "out/example"
area = "product"
destination = "bin/example"
mode = 0o755
```

```sh
mdp lock                     # Explicitly record the dependency declarations
mdp sync                     # Fetch and verify exactly the locked dependencies
mdp build                    # Run this project's argv, with its own incremental cache
mdp pack --output out/example.tar.mdp
mdp verify out/example.tar.mdp
```

`build` does not resolve dependencies or silently rewrite the lock. It executes
only the explicit local `[build].command` without a shell. A failed command is a
failed build. Build scripts are trusted project code, not sandboxed code.
`pack` packages already-built files; it does not build them or guarantee that
they are up to date with source changes. Keep the producer's build/package steps
in its own CI until source-to-product freshness receipts are implemented.

## Lock SDKs and other dependencies

Append explicit dependencies to `mdp.toml`, then run `mdp lock` and commit
`mdp.lock` (versioned JSON, despite the extension). Updates are deliberate; MDP
does not follow a moving `latest` version.

```toml
[[dependencies]]
name = "sdk"
kind = "sdk"
version = "27.0-dp.1-shared.10"
host = "x86_64-linux"
target = "x86_64-mochios"
url = "https://example.org/sdk.tar.gz"
sha256 = "REPLACE_WITH_THE_REVIEWED_64_CHARACTER_SHA256"
```

Kinds are `sdk`, `library`, `tool`, and `artifact`. A dependency has either an HTTPS
`url` or a project-relative `path`, plus a version and SHA-256. Machine-specific
absolute paths are forbidden. `sync` caches verified **opaque files** under
`.mdp/cache/<sha256>`. It does not extract SDKs or install software globally.
The build receives `MDP_DEP_SDK` pointing at the verified archive; the project's
build script can hand it to its existing SDK installer (for mochiOS, `komeup`).
Dependency names become uppercase environment variables with punctuation replaced
by underscores; collisions are rejected. `MDP_LOCK_FILE` identifies the lock.
The MDP records the declared dependencies and the hash of that lock. This records
inputs declared by the build, not an attestation of which compiler actually ran.

SDK versions are not runtime ABI versions. Assembly compares the product's explicit
target and ABI, not whether all producers used the same SDK release. Dependencies
of kind `artifact` must also be present in the composition at the exact declared
version and archive digest. Other kinds describe build inputs only.

## Compose artifacts without building

Put `mdp.assembly.toml` beside the incoming artifacts:

```toml
format = 1
target = "x86_64-example"
abi = "example-1"

[[artifacts]]
path = "incoming/example.tar.mdp"
```

```sh
mdp lock-assembly             # Review and commit mdp.assembly.lock
mdp assemble --output out/release-001
```

The output is a new directory containing:

```text
out/release-001/
  receipt.json               # Input archive identities and assembly-lock hash
  tree/
    product/bin/example
```

The assembly lock pins archive hashes and package identities. Missing or changed
inputs fail rather than trigger builds. Destination collisions, missing artifact
dependencies, and target/ABI mismatches fail before publishing any output.
Assembly stages on the output filesystem and renames the result into place;
existing outputs are never replaced. Product code consumes `tree/<area>` and
owns subsequent image generation, signing, packaging, or deployment. For example,
an application producer can put an MPKG inside an MDP without MDP interpreting it.

## Format 1

`.tar.mdp` is an **uncompressed tar**. The first entry is `manifest.json`; the
remaining entries are the exactly declared regular files under `payload/`.
Manifest metadata includes the package identity, target, ABI, dependencies, lock
hash, and each file's area, relative destination, size, mode, and SHA-256.
Areas are generic names; they do not grant permission to write to host paths.

Archive creation is reproducible: fixed ownership/timestamps, stable JSON, and
explicit file order. Modes are restricted to 0644 and 0755. Paths are canonical
relative POSIX paths. Links, devices, sparse files, undeclared members, duplicates,
and parent traversal are rejected. Format 1 does **not** support symlinks. Format 2 adds verified, same-area file
aliases; see WORKSPACES.md. Absolute host paths are not interpreted as destinations. Hash locks
provide integrity relative to trusted locks, not publisher authentication; review
locks and transport them through a trusted source-control/release process.

Workspace scheduling, pinned SDK contexts and trusted project-owned pipelines are
implemented; see [WORKSPACES.md](WORKSPACES.md). Publisher signatures, automatic
version selection and product-specific image generation are not implemented.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
