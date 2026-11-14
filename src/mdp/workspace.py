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
    head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    changes = subprocess.check_output(['git', '-C', str(path), 'diff', 'HEAD', '--binary', '--', '.',
                                       ':!Cargo.lock', ':!mdp.lock'])
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
    locks = {}
    for name in ('Cargo.lock', 'mdp.lock'):
        if (path / name).is_file():
            locks[name] = digest(path / name)
    return {'revision': head, 'working_tree': hasher.hexdigest(), 'resolution': locks}


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
    lock_path = root / 'mdp.workspace.lock'
    require(lock_path.is_file(), 'workspace lock missing; run mdp workspace lock')
    locked = json.loads(lock_path.read_text())
    inputs = profile_state(root, data)
    require(locked.get('format') == 1 and locked.get('inputs') == inputs, 'workspace configuration/SDK inputs changed; run mdp workspace lock')
    require(set(locked.get('projects', {})) == set(projects), 'workspace registry changed; run mdp workspace lock')
    states = {name: source_state(p['source']) for name, p in projects.items()}
    for name, state in states.items():
        expected = locked['projects'][name]
        require(all(state[k] == expected[k] for k in ('revision', 'working_tree')),
                f'{name}: source changed; run mdp workspace lock after reviewing your changes')
    names = order(projects, selected)
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
            states[name] = source_state(project['source'])
        dependency_receipts = {dep: receipts.get(dep) for dep in cfg.get('requires', []) if not projects[dep].get('source_only', False)}
        # All source pins are part of the SDK context; per-project dependencies
        # additionally pin generated native SDK and framework products.
        source_names = {name, *cfg.get('sources', [])}
        require(source_names <= set(states), f'{name}: unknown SDK source dependency')
        def input_key():
            return hashlib.sha256(encoded({'sources': {key: states[key] for key in sorted(source_names)}, 'inputs': inputs,
                                          'dependencies': dependency_receipts, 'command': cfg['command']})).hexdigest()
        key = input_key()
        receipt = receipts.get(name, {})
        artifact = project['source'] / relative(cfg['artifact']) if cfg.get('artifact') else None
        outputs = [root / relative(value) for value in cfg.get('outputs', [])]
        if receipt.get('input_sha256') == key and all(p.exists() for p in outputs) and (
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
        after = source_state(project['source'])
        require(all(after[k] == states[name][k] for k in ('revision', 'working_tree')),
                f'{name}: source changed during its build; output is not accepted')
        states[name] = after
        state = {'input_sha256': input_key(), 'source': after, 'outputs': cfg.get('outputs', [])}
        if artifact is not None:
            manifest = verify(artifact)
            state.update(artifact=artifact.relative_to(root).as_posix(), sha256=digest(artifact), package=manifest['package'])
        require(all(p.exists() for p in outputs), f'{name}: declared output is missing')
        receipts[name] = state
        atomic_json(receipts_path, receipts)
    require(profile_state(root, data) == inputs, "workspace configuration/SDK inputs changed during build")
    for name, project in projects.items():
        after = source_state(project["source"])
        require(all(after[k] == states[name][k] for k in ("revision", "working_tree")),
                f"{name}: source changed during workspace build; composition lock is not updated")
    if receipts:
        valid = {}
        for name in order(projects, list(projects)):
            cfg = projects[name]['config']['build']
            deps = [dep for dep in cfg.get('requires', []) if not projects[dep].get('source_only', False)]
            if any(dep not in valid for dep in deps):
                continue
            key = hashlib.sha256(encoded({'sources': {key: source_state(projects[key]['source']) for key in sorted({name, *cfg.get('sources', [])})},
                                          'inputs': inputs, 'dependencies': {dep: valid[dep] for dep in deps}, 'command': cfg['command']})).hexdigest()
            receipt = receipts.get(name, {})
            if receipt.get('input_sha256') != key:
                continue
            artifact = root / receipt['artifact'] if 'artifact' in receipt else None
            if artifact is not None and (not artifact.is_file() or digest(artifact) != receipt.get('sha256')):
                continue
            if not all((root / relative(value)).exists() for value in cfg.get('outputs', [])):
                continue
            valid[name] = receipt
        artifacts = [r for r in valid.values() if 'artifact' in r]
        require(artifacts, 'workspace built no composition artifacts')
        assembly = {'format': 1, 'target': data['target'], 'abi': data['abi'],
                    'artifacts': [{'path': r['artifact'], 'sha256': r['sha256'], 'package': r['package']} for r in artifacts]}
        atomic_json(root / 'mdp.assembly.lock', assembly)
