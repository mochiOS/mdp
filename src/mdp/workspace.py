"""Product-independent source locking and project-owned build orchestration."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import fcntl

from .cli import Error, atomic_json, digest, encoded, identifier, relative, require, verify


def read(path):
    """Load a versioned workspace configuration and its explicit project registry."""
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text())
    require(data.get('format') == 1, 'workspace requires format = 1')
    root = path.parent
    data['_manifest_sha256'] = digest(path)
    projects = {}
    for item in data.get('projects', []):
        name = identifier(item['name'])
        require(name not in projects, f'duplicate workspace project: {name}')
        source = root / relative(item['path'])
        require(source.resolve().is_relative_to(root), 'project escapes workspace')
        manifest = source / relative(item.get('manifest', 'mdp.toml'))
        cfg = tomllib.loads(manifest.read_text()) if manifest.is_file() else {}
        if item.get('source_only', False):
            require(not cfg.get('build'), f'{name}: source-only entry has a build command')
        else:
            require(cfg.get('format') == 1 and cfg.get('build', {}).get('command'), f'{name}: missing project-owned build entry')
        projects[name] = dict(item, source=source, manifest=manifest, config=cfg)
    require(projects, 'workspace contains no projects')
    return root, data, projects


def source_state(path):
    """Describe actual Git input, including local edits, without including build caches."""
    for _ in range(4):
        head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        index = subprocess.check_output(['git', '-C', str(path), 'ls-files', '--stage', '-z'])
        try:state = read_source_state(path, head)
        except FileNotFoundError:continue
        after = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        after_index = subprocess.check_output(['git', '-C', str(path), 'ls-files', '--stage', '-z'])
        if head == after and index == after_index:return state
    raise Error(f'{path}: Git state kept changing while reading source inputs; retry the build')


def read_source_state(path, head):
    """Read against one captured revision; the caller verifies index/revision stability."""
    tree = subprocess.check_output(['git', '-C', str(path), 'ls-tree', '-r', '-z', head])
    entries = {}
    for record in tree.split(b'\0'):
        if not record:continue
        metadata, raw = record.split(b'\t', 1)
        name = os.fsdecode(raw)
        if Path(name).name in ('Cargo.lock', 'mdp.lock'):continue
        mode, kind, oid = metadata.decode().split()
        entries[name] = [mode, oid]
    tracked = hashlib.sha256(encoded(entries)).hexdigest()
    changes = subprocess.check_output(['git', '-C', str(path), 'diff', head, '--binary', '--', '.',
                                       ':!Cargo.lock', ':!mdp.lock', ':(exclude,glob)**/Cargo.lock', ':(exclude,glob)**/mdp.lock'])
    hasher = hashlib.sha256(changes)
    untracked = subprocess.check_output(['git', '-C', str(path), 'ls-files', '--others', '--exclude-standard', '-z'])
    for raw in sorted(untracked.split(b'\0')):
        if not raw:
            continue
        name = os.fsdecode(raw)
        if name.startswith(('out/', 'target/', '.mdp/')) or Path(name).name in ('Cargo.lock', 'mdp.lock'):
            continue
        source = path / name
        if source.is_file() and not source.is_symlink():
            hasher.update(raw + b'\0' + bytes.fromhex(digest(source)))
    changed = subprocess.check_output(['git', '-C', str(path), 'diff', head, '--no-renames', '--name-only', '-z'])
    object_format = subprocess.check_output(['git', '-C', str(path), 'rev-parse', '--show-object-format'], text=True).strip()
    for raw in set(changed.split(b'\0')) | set(untracked.split(b'\0')):
        if not raw:continue
        name = os.fsdecode(raw)
        if name.startswith(('out/', 'target/', '.mdp/')) or Path(name).name in ('Cargo.lock', 'mdp.lock'):continue
        file = path / name
        if file.is_symlink():
            content = os.fsencode(os.readlink(file))
            blob = hashlib.new(object_format, b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
            entries[name] = ['120000', blob]
        elif file.is_file():
            hasher_content = hashlib.new(object_format, b'blob ' + str(file.stat().st_size).encode() + b'\0')
            with file.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):hasher_content.update(chunk)
            entries[name] = ['100755' if file.stat().st_mode & 0o111 else '100644', hasher_content.hexdigest()]
        elif file.is_dir() and name in entries and entries[name][0] == '160000':
            entries[name] = ['160000', source_state(file)['content_sha256']]
        else:entries.pop(name, None)
    content_identity = hashlib.sha256(encoded(entries)).hexdigest()
    locks, graphs = {}, {}
    listed = subprocess.check_output(['git', '-C', str(path), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'])
    names = {os.fsdecode(raw) for raw in listed.split(b'\0') if raw and Path(os.fsdecode(raw)).name in ('Cargo.lock', 'mdp.lock')}
    names.update(name for name in ('Cargo.lock', 'mdp.lock') if (path / name).is_file())
    for name in sorted(names):
        if name.startswith(('out/', 'target/', '.mdp/')):continue
        if (path / name).is_file():
            raw_lock = (path / name).read_bytes()
            locks[name] = hashlib.sha256(raw_lock).hexdigest()
            if Path(name).name == 'Cargo.lock':
                try:graph = tomllib.loads(raw_lock.decode())
                except tomllib.TOMLDecodeError:
                    graphs[name] = locks[name]
                    continue
                if 'patch' in graph:
                    graph['patch'].pop('unused', None)
                    if not graph['patch']:del graph['patch']
                graphs[name] = hashlib.sha256(encoded(graph)).hexdigest()
            else:graphs[name] = locks[name]
    return {'revision': head, 'tracked_tree': tracked, 'content_sha256': content_identity, 'working_tree': hasher.hexdigest(), 'resolution': locks, 'resolution_graph': graphs}


def build_key(name, cfg, states, inputs, dependencies, root):
    """Hash consumed source/lock inputs, excluding unrelated source-workspace locks."""
    sources = {}
    for key in sorted({name, *cfg.get('sources', [])}):
        state = states[key]
        sources[key] = {'content_sha256': state['content_sha256']}
        if key == name and cfg.get('resolution', True):
            sources[key]['resolution'] = state.get('resolution_graph', state['resolution'])
    files = file_inputs(root, cfg)
    return hashlib.sha256(encoded({'sources': sources, 'inputs': inputs, 'files': files,
                                  'dependencies': {key: dependency_identity(value) for key,value in dependencies.items()},
                                  'command': cfg['command']})).hexdigest()


def dependency_identity(receipt):
    """Depend on exchanged products, keeping producer source diagnostics out of consumer keys."""
    if receipt is None:return None
    if 'artifact' in receipt:
        return {key:receipt[key] for key in ('artifact','sha256','package')} | {'outputs':receipt.get('output_hashes',{})}
    if receipt.get('output_hashes'):
        return {'outputs':receipt['output_hashes']}
    return {'input_sha256':receipt['input_sha256']}


def output_state(root, cfg):
    """Measure declared generated files; missing or invalid outputs cannot be cached."""
    result={}
    for value in cfg.get('outputs',[]):
        path=root/relative(value)
        if not path.is_file():return None
        result[value]={'sha256':digest(path),'mode':path.stat().st_mode & 0o777,
                       'link':os.readlink(path) if path.is_symlink() else None}
    return result


def file_inputs(root, cfg):
    """Measure existing producer inputs, including images made by a separate composer."""
    files = {}
    for name in cfg.get('inputs', []):
        source = root / relative(name)
        require(source.is_file() and source.resolve().is_relative_to(root), f'missing or escaping producer input: {name}')
        files[name] = digest(source)
    return files


def profile_state(root, data):
    """Pin explicitly declared configuration/SDK policy files by content."""
    result = {name: digest(root / relative(name)) for name in data.get('inputs', [])}
    result['workspace_config'] = data['_manifest_sha256']
    for item in data.get('external_inputs', []):
        source = root / relative(item['path'])
        require(source.is_dir(), f'external SDK/input missing: {source}')
        inventory = {}
        for file in sorted(source.rglob('*')):
            if file.is_file():
                require(file.resolve().is_relative_to(source.resolve()), 'external input link escapes its tree')
                inventory[file.relative_to(source).as_posix()] = digest(file)
        require(inventory, f'empty external input: {source}')
        result['external:' + identifier(item['name'])] = hashlib.sha256(encoded(inventory)).hexdigest()
    return result


def lock(path):
    """Write an explicit source/SDK lock; never resolve moving Git branches."""
    root, data, projects = read(path)
    value = {'format': 1, 'inputs': profile_state(root, data),
             'projects': {name: source_state(p['source']) for name, p in projects.items()}}
    atomic_json(root / 'mdp.workspace.lock', value)
    return value


def order(projects, selected=None):
    """Topologically order declared build dependencies, rejecting missing nodes/cycles."""
    result, visiting, visited = [], set(), set()
    def visit(name):
        require(name in projects, f'unknown workspace dependency: {name}')
        require(name not in visiting, f'workspace dependency cycle at {name}')
        if name in visited:
            return
        visiting.add(name)
        for dep in projects[name]['config'].get('build', {}).get('requires', []):
            visit(dep)
        visiting.remove(name)
        visited.add(name)
        if not projects[name].get('source_only', False):
            result.append(name)
    for name in selected if selected is not None else [name for name, p in projects.items() if not p.get('optional', False)]:
        visit(name)
    return result


def source_closure(projects, name):
    """Collect transitive source inputs; shared protocols may form source cycles."""
    pending, result = [name], set()
    while pending:
        current = pending.pop()
        require(current in projects, f'unknown SDK source dependency: {current}')
        if current in result:continue
        result.add(current)
        pending.extend(projects[current]['config'].get('build', {}).get('sources', []))
    return result


def build(path, selected=None, jobs=4, dry_run=False):
    """Serialize workspace builds while keeping producer logic in its own repository."""
    root = Path(path).resolve().parent
    directory = root / '.mdp'
    directory.mkdir(exist_ok=True)
    with (directory / 'workspace-build.lock').open('a') as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('another workspace build is active') from None
        return build_locked(path, selected, jobs, dry_run)


def build_locked(path, selected=None, jobs=4, dry_run=False):
    """Run project-owned entry points and record exact output identities for composition."""
    root, data, projects = read(path)
    closures = {name: source_closure(projects, name) - {name} for name in projects}
    for name, project in projects.items():
        if 'build' in project['config']:
            project['config']['build'] = dict(project['config']['build'], sources=sorted(closures[name]))
    lock_path = root / 'mdp.workspace.lock'
    require(lock_path.is_file(), 'workspace lock missing; run mdp workspace lock')
    locked = json.loads(lock_path.read_text())
    inputs = profile_state(root, data)
    require(locked.get('format') == 1 and locked.get('inputs') == inputs, 'workspace configuration/SDK inputs changed; run mdp workspace lock')
    require(set(locked.get('projects', {})) == set(projects), 'workspace registry changed; run mdp workspace lock')
    names = order(projects, selected)
    consumed = set(names).union(*(closures[name] for name in names))
    states = {name: source_state(projects[name]['source']) for name in consumed}
    for name, state in states.items():
        expected = locked['projects'][name]
        require(all(state[k] == expected[k] for k in ('content_sha256',)),
                f'{name}: source changed; run mdp workspace lock after reviewing your changes')
    if dry_run:
        for name in names:
            print(name + ': ' + json.dumps(projects[name]['config']['build']['command']))
        return
    receipts_path = root / '.mdp/builds.json'
    receipts = json.loads(receipts_path.read_text()) if receipts_path.is_file() else {}
    environment = os.environ.copy()
    environment['MDP_JOBS'] = str(jobs)
    environment['MDP_WORKSPACE'] = str(root)
    for key, value in data.get('environment', {}).items():
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) and isinstance(value, str), 'invalid workspace environment')
        environment[key] = value.replace('{workspace}', str(root))
    for name in names:
        project = projects[name]
        cfg = project['config']['build']
        if cfg.get('sdk_context'):
            context_path = root / relative(data['sdk_context'])
            require(context_path.is_file(), 'SDK context producer has not completed')
            context = json.loads(context_path.read_text())
            from .cli import dependencies
            consumer_lock = {'format': 1, 'dependencies': dependencies(project['config']), 'sdk': context['sdk']}
            consumer_path = project['source'] / 'mdp.lock'
            if not consumer_path.is_file() or json.loads(consumer_path.read_text()) != consumer_lock:
                atomic_json(consumer_path, consumer_lock)
                print(f'[mdp SDK lock] {name}', flush=True)
            refreshed = source_state(project['source'])
            require(refreshed['content_sha256'] == states[name]['content_sha256'], f'{name}: source changed before its build')
            states[name] = refreshed
        dependency_receipts = {dep: receipts.get(dep) for dep in cfg.get('requires', []) if not projects[dep].get('source_only', False)}
        # All source pins are part of the SDK context; per-project dependencies
        # additionally pin generated native SDK and framework products.
        source_names = {name, *cfg.get('sources', [])}
        require(source_names <= set(states), f'{name}: unknown SDK source dependency')
        def input_key():
            return build_key(name, cfg, states, inputs, dependency_receipts, root)
        key = input_key()
        consumed_files = file_inputs(root, cfg)
        receipt = receipts.get(name, {})
        artifact = project['source'] / relative(cfg['artifact']) if cfg.get('artifact') else None
        outputs = [root / relative(value) for value in cfg.get('outputs', [])]
        measured_outputs=output_state(root,cfg)
        if receipt.get('input_sha256') == key and measured_outputs is not None and measured_outputs == receipt.get('output_hashes',{}) and (
                artifact is None or artifact.is_file() and digest(artifact) == receipt.get('sha256')):
            print(f'[mdp cached] {name}', flush=True)
            continue
        print(f'[mdp build] {name}', flush=True)
        command = cfg['command']
        require(isinstance(command, list) and all(isinstance(arg, str) for arg in command), 'build command must be argv')
        env = dict(environment, MDP_PROJECT=name)
        env['MDP_REQUIRES'] = json.dumps(order(projects, cfg.get('requires', [])))
        env['PATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PATH', '')
        subprocess.run(command, cwd=project['source'], env=env, check=True)
        require(file_inputs(root, cfg) == consumed_files, f'{name}: producer file inputs changed during build')
        after = source_state(project['source'])
        require(all(after[k] == states[name][k] for k in ('content_sha256',)),
                f'{name}: source changed during its build; output is not accepted')
        states[name] = after
        measured_outputs=output_state(root,cfg)
        require(measured_outputs is not None, f'{name}: declared output is missing or is not a file')
        state = {'input_sha256': input_key(), 'source': after, 'outputs': cfg.get('outputs', []), 'output_hashes':measured_outputs}
        if artifact is not None:
            manifest = verify(artifact)
            state.update(artifact=artifact.relative_to(root).as_posix(), sha256=digest(artifact), package=manifest['package'])
        require(all(p.exists() for p in outputs), f'{name}: declared output is missing')
        receipts[name] = state
        atomic_json(receipts_path, receipts)
    require(profile_state(root, data) == inputs, "workspace configuration/SDK inputs changed during build")
    for name in consumed:
        after = source_state(projects[name]["source"])
        require(after['content_sha256'] == states[name]['content_sha256'],
                f"{name}: source changed during workspace build; composition lock is not updated")
    if receipts:
        valid = {}
        for name in order(projects, list(projects)):
            cfg = projects[name]['config']['build']
            deps = [dep for dep in cfg.get('requires', []) if not projects[dep].get('source_only', False)]
            if any(dep not in valid for dep in deps):
                continue
            if name not in receipts:
                continue
            if any(not (root / relative(value)).is_file() for value in cfg.get('inputs', [])):
                continue
            current = {key: source_state(projects[key]['source']) for key in {name, *cfg.get('sources', [])}}
            key = build_key(name, cfg, current, inputs, {dep: valid[dep] for dep in deps}, root)
            receipt = receipts.get(name, {})
            if receipt.get('input_sha256') != key:
                continue
            artifact = root / receipt['artifact'] if 'artifact' in receipt else None
            if artifact is not None and (not artifact.is_file() or digest(artifact) != receipt.get('sha256')):
                continue
            measured_outputs=output_state(root,cfg)
            if measured_outputs is None or measured_outputs != receipt.get('output_hashes',{}):
                continue
            valid[name] = receipt
        artifacts = [r for r in valid.values() if 'artifact' in r]
        require(artifacts, 'workspace built no composition artifacts')
        assembly = {'format': 1, 'target': data['target'], 'abi': data['abi'],
                    'artifacts': [{'path': r['artifact'], 'sha256': r['sha256'], 'package': r['package']} for r in artifacts]}
        atomic_json(root / 'mdp.assembly.lock', assembly)
