"""Verified, deterministic artifact exchange. No product-specific image logic."""
import argparse
import hashlib
import io
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request

MAX_MANIFEST = 4 * 1024 * 1024
MAX_FILES = 100_000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class Error(Exception):
    """An invalid configuration, dependency, or artifact; safe to show to users."""


def require(condition, message):
    """Reject input when a format or safety invariant is not met."""
    if not condition:
        raise Error(message)


def digest(path):
    """Return the SHA-256 of a file with bounded memory use."""
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def encoded(value):
    """Serialize machine-owned metadata deterministically as UTF-8 JSON."""
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def read_json(path):
    """Read metadata, reporting JSON errors through the CLI error boundary."""
    with open(path, 'rb') as stream:
        return json.load(stream)


def relative(value):
    """Validate a canonical relative POSIX path, never an absolute host path."""
    require(isinstance(value, str) and value and '\\' not in value and '\x00' not in value,
            f'invalid relative path: {value!r}')
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(p not in ('', '.', '..') for p in value.split('/')),
            f'unsafe relative path: {value!r}')
    return value


def identifier(value):
    """Validate a portable project, dependency, or output-area identifier."""
    require(isinstance(value, str) and NAME.fullmatch(value), f'invalid identifier: {value!r}')
    return value


def atomic_json(path, value):
    """Replace a metadata file atomically, without following a destination symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded(value))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def config(path):
    """Load a versioned project configuration and its containing directory."""
    path = Path(path).resolve()
    with path.open('rb') as stream:
        value = tomllib.load(stream)
    require(value.get('format') == 1, 'mdp.toml requires format = 1')
    return path.parent, value


def dependencies(cfg):
    """Validate explicit immutable dependency specifications, including SDKs."""
    values = cfg.get('dependencies', [])
    require(isinstance(values, list), 'dependencies must be an array of tables')
    names = set()
    for item in values:
        name = identifier(item.get('name'))
        require(name not in names, f'duplicate dependency: {name}')
        names.add(name)
        require(item.get('kind') in ('sdk', 'library', 'artifact', 'tool'), f'{name}: invalid kind')
        require(isinstance(item.get('version'), str) and item['version'], f'{name}: version required')
        require(isinstance(item.get('sha256'), str) and SHA.fullmatch(item['sha256']),
                f'{name}: an explicit sha256 is required')
        require(('path' in item) != ('url' in item), f'{name}: specify exactly one of path or url')
        if 'path' in item:
            relative(item['path'])
        else:
            parsed = urllib.parse.urlsplit(item['url'])
            require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password, f'{name}: dependency URL must be HTTPS without credentials')
    return values


def sdk_context(path):
    """Validate an explicitly selected local SDK context and its immutable inputs."""
    value = read_json(path)
    require(value.get('format') == 1 and isinstance(value.get('sdk'), dict), 'invalid SDK context')
    require(isinstance(value['sdk'].get('id'), str) and SHA.fullmatch(value['sdk']['id']), 'invalid SDK context identity')
    for item in value.get('artifacts', []):
        require(digest(item['path']) == item['sha256'], 'SDK artifact changed; rebuild its context')
    from .workspace import source_state
    for source in value.get('sources', []):
        state = source_state(Path(source['path']))
        require(all(state[k] == source[k] for k in ('revision', 'working_tree')), 'SDK source changed; rebuild its context')
    require(isinstance(value.get('environment'), dict) and all(isinstance(k, str) and isinstance(v, str)
            for k, v in value['environment'].items()), 'SDK environment must contain strings')
    return value


def lock_project(project, context=None):
    """Record reviewed dependency identities; never resolve a moving latest version."""
    root, cfg = config(project)
    deps = dependencies(cfg)
    for item in deps:
        if 'path' in item:
            source = (root / item['path']).resolve()
            require(source.is_relative_to(root) and source.is_file(), f"missing dependency: {item['name']}")
            require(digest(source) == item['sha256'], f"hash mismatch: {item['name']}")
    result = {'format': 1, 'dependencies': deps}
    if context:
        result['sdk'] = sdk_context(context)['sdk']
    atomic_json(root / 'mdp.lock', result)
    return result


def locked(project, context=None):
    """Require a committed lock matching the project's exact dependency declarations."""
    root, cfg = config(project)
    path = root / 'mdp.lock'
    require(path.is_file(), 'mdp.lock is missing; review dependencies and run mdp lock')
    value = read_json(path)
    expected = {'format': 1, 'dependencies': dependencies(cfg)}
    if context:
        expected['sdk'] = sdk_context(context)['sdk']
    require(value == expected,
            'mdp.lock is stale; review dependencies and run mdp lock')
    return root, cfg, value


def sync(project):
    """Fetch locked opaque dependency archives into a verified content-addressed cache."""
    root, cfg, lock = locked(project)
    cache = root / '.mdp' / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    for dep in lock['dependencies']:
        target = cache / dep['sha256']
        if target.is_file() and digest(target) == dep['sha256']:
            continue
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                if 'path' in dep:
                    source = (root / dep['path']).resolve()
                    require(source.is_relative_to(root), 'dependency escapes project')
                    with source.open('rb') as input_stream:
                        shutil.copyfileobj(input_stream, stream)
                else:
                    # A redirect must retain HTTPS; never downgrade to plaintext or file:.
                    class HTTPSOnly(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, req, fp, code, msg, headers, newurl):
                            require(urllib.parse.urlsplit(newurl).scheme == 'https',
                                    'dependency redirect must remain HTTPS')
                            return super().redirect_request(req, fp, code, msg, headers, newurl)
                    opener = urllib.request.build_opener(HTTPSOnly())
                    with opener.open(dep['url'], timeout=60) as response:
                        shutil.copyfileobj(response, stream)
                stream.flush()
                require(digest(temporary) == dep['sha256'], f"hash mismatch: {dep['name']}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"verified dependency: {dep['name']} {dep['version']}")
    return root, cfg, lock


def build(project, context=None):
    """Run only the selected project's explicit argv, with locked dependency paths."""
    root, cfg, lock = locked(project, context)
    command = cfg.get('build', {}).get('command')
    require(isinstance(command, list) and command and all(isinstance(x, str) for x in command),
            '[build].command must be a nonempty argv array')
    env = os.environ.copy()
    if context:
        env.update(sdk_context(context)['environment'])
        env['MDP_SDK_DESCRIPTOR'] = str(Path(context).resolve())
    env['MDP_LOCK_FILE'] = str(root / 'mdp.lock')
    env_names = set()
    for dep in lock['dependencies']:
        path = root / '.mdp' / 'cache' / dep['sha256']
        require(path.is_file() and digest(path) == dep['sha256'],
                f"dependency {dep['name']} is unavailable or corrupt; run mdp sync")
        key = 'MDP_DEP_' + re.sub('[^A-Za-z0-9]', '_', dep['name']).upper()
        require(key not in env_names, f'dependency environment name collision: {key}')
        env_names.add(key)
        env[key] = str(path)
    subprocess.run(command, cwd=root, env=env, check=True)
    if context:
        require(sdk_context(context)["sdk"] == lock["sdk"], "SDK changed during build")


def check_paths(files):
    """Reject duplicate and ancestor/descendant destination conflicts before writing."""
    seen = set()
    for file in files:
        area = identifier(file.get('area'))
        destination = relative(file.get('destination'))
        path = area + '/' + destination
        require(path not in seen, f'duplicate destination: {path}')
        seen.add(path)
    for path in seen:
        for parent in PurePosixPath(path).parents:
            require(str(parent) not in seen, f'file/directory collision: {path}')


def check_links(files):
    """Validate local file aliases, excluding area escapes, missing targets and cycles."""
    records = {f['area'] + '/' + f['destination']: f for f in files}
    resolved = {}
    for key, item in records.items():
        if item.get('type') != 'symlink':
            continue
        target = item.get('target')
        require(isinstance(target, str) and target and not target.startswith('/') and '\\' not in target
                and '\x00' not in target, 'invalid symbolic link target')
        destination = posixpath.normpath(posixpath.join(posixpath.dirname(item['destination']), target))
        relative(destination)
        selected = item['area'] + '/' + destination
        require(selected in records, 'symbolic link must target a declared file in the same area')
        resolved[key] = selected
    for start in resolved:
        seen = set()
        current = start
        while current in resolved:
            require(current not in seen and len(seen) < 128, 'symbolic link cycle or excessive depth')
            seen.add(current)
            current = resolved[current]


def pack(project, output):
    """Create a reproducible plain-tar MDP containing declared regular files only."""
    root, cfg, lock = locked(project)
    package = cfg.get('package', {})
    identifier(package.get('name'))
    require(all(isinstance(package.get(k), str) and package[k] for k in ('version', 'target', 'abi')),
            'package requires version, target and abi')
    files = cfg.get('files', [])
    require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, 'declare at least one file')
    check_paths(files)
    records = []
    sources = []
    for index, file in enumerate(files):
        source = root / relative(file.get('source'))
        require(not source.is_symlink() and source.resolve().is_relative_to(root) and source.is_file(),
                f'file must be a regular file inside project: {source}')
        mode = file.get('mode', 0o644)
        require(mode in (0o644, 0o755), 'file mode must be 0o644 or 0o755')
        records.append({'source': f'payload/{index:06d}', 'area': file['area'],
                        'destination': file['destination'], 'mode': mode,
                        'size': source.stat().st_size, 'sha256': digest(source)})
        sources.append(source)
    for link in cfg.get('links', []):
        records.append(dict(type='symlink', area=link['area'], destination=link['destination'], target=link['target']))
    check_paths(records)
    check_links(records)
    manifest = {'format': 2 if cfg.get('links') else 1, 'package': package, 'files': records,
                'dependencies': lock['dependencies'], 'lock_sha256': digest(root / 'mdp.lock')}
    raw = encoded(manifest)
    require(len(raw) <= MAX_MANIFEST, 'manifest is too large')
    output = Path(output).resolve()
    require(output not in [p.resolve() for p in sources], 'output cannot replace an input')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with tarfile.open(temporary, 'w', format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo('manifest.json')
            info.size = len(raw)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(raw))
            for record, source in zip(records, sources):
                info = tarfile.TarInfo(record['source'])
                info.size = record['size']
                info.mode = record['mode']
                with source.open('rb') as stream:
                    archive.addfile(info, stream)
        verify(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'created {output}')


def verify(path, staging=None, areas=None):
    """Validate every archive member; optionally copy verified data into a fresh stage."""
    with tarfile.open(path, 'r:') as archive:
        first = archive.next()
        require(first is not None and first.name == 'manifest.json' and first.isfile()
                and 0 < first.size <= MAX_MANIFEST, 'first member must be a bounded manifest.json')
        manifest = json.load(archive.extractfile(first))
        require(manifest.get('format') in (1, 2), 'unsupported MDP format')
        package = manifest.get('package', {})
        identifier(package.get('name'))
        require(all(isinstance(package.get(k), str) and package[k] for k in ('version', 'target', 'abi')),
                'invalid package identity')
        require(isinstance(manifest.get('lock_sha256'), str) and SHA.fullmatch(manifest['lock_sha256']),
                'invalid dependency lock digest')
        dependencies(manifest)
        files = manifest.get('files')
        require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, 'invalid file count')
        check_paths(files)
        check_links(files)
        expected = {}
        for item in files:
            if item.get('type') == 'symlink':
                require(manifest['format'] == 2, 'symlinks require format 2')
                continue
            require(item.get('type', 'file') == 'file', 'unknown MDP entry type')
            source = relative(item.get('source'))
            require(source.startswith('payload/') and source not in expected, 'invalid or duplicate source')
            require(type(item.get('size')) is int and item['size'] >= 0, 'invalid file size')
            require(item.get('mode') in (0o644, 0o755), 'unsupported file mode')
            require(isinstance(item.get('sha256'), str) and SHA.fullmatch(item['sha256']), 'invalid sha256')
            expected[source] = item
        seen = set()
        while (member := archive.next()) is not None:
            require(member.isfile() and not member.issparse(), 'links, sparse files and special entries are forbidden')
            require(member.name in expected and member.name not in seen, f'unexpected member: {member.name}')
            record = expected[member.name]
            require(member.size == record['size'], f'size mismatch: {member.name}')
            seen.add(member.name)
            hasher = hashlib.sha256()
            output = None
            try:
                if staging is not None and (areas is None or record['area'] in areas):
                    destination = staging / record['area'] / record['destination']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    output = destination.open('xb')
                with archive.extractfile(member) as stream:
                    while chunk := stream.read(1024 * 1024):
                        hasher.update(chunk)
                        if output:
                            output.write(chunk)
            finally:
                if output:
                    output.close()
            require(hasher.hexdigest() == record['sha256'], f'hash mismatch: {member.name}')
            if output is not None:
                os.chmod(destination, record['mode'])
        require(seen == set(expected), 'archive is missing declared files')
        if staging is not None:
            for item in files:
                if item.get('type') == 'symlink' and (areas is None or item['area'] in areas):
                    path = staging / item['area'] / item['destination']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(item['target'])
    return manifest


def composition_lock(config_path):
    """Pin explicitly selected MDPs; this operation never invokes a build."""
    root, cfg = config(config_path)
    artifacts = []
    names = set()
    for entry in cfg.get('artifacts', []):
        source = root / relative(entry.get('path'))
        require(source.resolve().is_relative_to(root), 'artifact escapes composition directory')
        manifest = verify(source)
        name = manifest['package']['name']
        require(name not in names, f'duplicate package: {name}')
        names.add(name)
        artifacts.append({'path': entry['path'], 'sha256': digest(source), 'package': manifest['package']})
    require(artifacts, 'composition has no artifacts')
    value = {'format': 1, 'target': cfg.get('target'), 'abi': cfg.get('abi'), 'artifacts': artifacts}
    require(isinstance(value['target'], str) and isinstance(value['abi'], str), 'composition requires target and abi')
    atomic_json(root / 'mdp.assembly.lock', value)


def assemble(lock_path, destination, areas=None):
    """Assemble locked artifacts atomically into a new directory, never run commands."""
    lock_path = Path(lock_path).resolve()
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    require(lock.get('format') == 1 and lock.get('artifacts'), 'invalid assembly lock')
    destination = Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'output already exists; choose a new directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mdp-assemble-', dir=destination.parent) as work:
        work = Path(work)
        stage = work / 'tree'
        stage.mkdir()
        manifests = []
        records = []
        for index, entry in enumerate(lock['artifacts']):
            source = lock_path.parent / relative(entry.get('path'))
            require(source.resolve().is_relative_to(lock_path.parent), 'artifact escapes lock directory')
            # Snapshot inputs once: validation and extraction must use the same bytes.
            snapshot = work / f'{index}.tar.mdp'
            shutil.copyfile(source, snapshot)
            require(digest(snapshot) == entry['sha256'], f'hash mismatch: {source}')
            manifest = verify(snapshot)
            package = manifest['package']
            require(package == entry['package'], 'package identity differs from assembly lock')
            require(package['target'] == lock['target'] and package['abi'] == lock['abi'],
                    f"incompatible target or ABI: {package['name']}")
            records.extend(manifest['files'])
            check_paths(records)
            manifests.append((snapshot, manifest))
        names = [m['package']['name'] for _, m in manifests]
        require(len(names) == len(set(names)), 'duplicate package in assembly')
        provided = {m['package']['name']: m['package']['version'] for _, m in manifests}
        for snapshot, manifest in manifests:
            for dep in manifest['dependencies']:
                if dep['kind'] == 'artifact':
                    require(provided.get(dep['name']) == dep['version'], f"missing artifact dependency: {dep['name']}")
                    matching = next(p for p, m in manifests if m['package']['name'] == dep['name'])
                    require(digest(matching) == dep['sha256'], f"artifact dependency hash mismatch: {dep['name']}")
            verify(snapshot, stage, areas)
        # Receipt is a sibling so it cannot collide with payload destinations.
        receipt = {'format': 1, 'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(), 'artifacts': lock['artifacts'],
                   'areas': sorted(areas) if areas is not None else None}
        atomic_json(work / 'receipt.json', receipt)
        bundle = work / 'result'
        bundle.mkdir()
        stage.rename(bundle / 'tree')
        (work / 'receipt.json').rename(bundle / 'receipt.json')
        require(not destination.exists(), 'output appeared during assembly')
        bundle.rename(destination)
    print(f'assembled {destination / "tree"}')


def main(argv=None):
    """Execute the CLI and convert expected failures into concise actionable errors."""
    parser = argparse.ArgumentParser(description='Multi-project development tool')
    parser.add_argument('--version', action='version', version='mdp 0.2.0')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('lock', 'sync', 'build', 'pack'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--project', default='mdp.toml')
        if name in ('lock', 'build'):
            cmd.add_argument('--context', help='explicit local SDK context, pinned in mdp.lock')
        if name == 'pack':
            cmd.add_argument('--output', required=True)
    cmd = commands.add_parser('verify')
    cmd.add_argument('archive')
    cmd = commands.add_parser('lock-assembly')
    cmd.add_argument('--config', default='mdp.assembly.toml')
    cmd = commands.add_parser('assemble')
    cmd.add_argument('--lock', default='mdp.assembly.lock')
    cmd.add_argument('--output', required=True)
    cmd.add_argument('--area', action='append', help='verify everything but materialize only this area')
    cmd = commands.add_parser('pipeline', help='run a trusted project-owned pipeline')
    cmd.add_argument('--recipe', default='mdp.recipe.json')
    cmd = commands.add_parser('workspace', help='lock sources and build independent projects')
    sub = cmd.add_subparsers(dest='operation', required=True)
    for name in ('lock', 'build'):
        item = sub.add_parser(name)
        item.add_argument('--file', default='mdp.workspace.toml')
        if name == 'build':
            item.add_argument('--select', action='append')
            item.add_argument('-j', '--jobs', type=int, default=4)
            item.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'lock':
            lock_project(args.project, args.context)
        elif args.command == 'sync':
            sync(args.project)
        elif args.command == 'build':
            build(args.project, args.context)
        elif args.command == 'pack':
            pack(args.project, args.output)
        elif args.command == 'verify':
            manifest = verify(args.archive)
            print(f"verified {manifest['package']['name']} {manifest['package']['version']}")
        elif args.command == 'lock-assembly':
            composition_lock(args.config)
        elif args.command == 'assemble':
            if args.area:
                for area in args.area:identifier(area)
            assemble(args.lock, args.output, set(args.area) if args.area else None)
        elif args.command == 'pipeline':
            from . import pipeline
            pipeline.run(args.recipe)
        elif args.command == 'workspace':
            from . import workspace
            if args.operation == 'lock':
                workspace.lock(args.file)
            else:
                require(args.jobs > 0, 'jobs must be positive')
                workspace.build(args.file, args.select, args.jobs, args.dry_run)
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError,
            subprocess.CalledProcessError) as error:
        print(f'mdp: {error}', file=sys.stderr)
        return 1
