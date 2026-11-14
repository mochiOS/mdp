"""Trusted project-owned shell pipelines and explicit artifact exports."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import fnmatch
import fcntl

from .cli import Error, digest, identifier, pack, relative, require, lock_project


def expand(value, variables):
    """Expand declared uppercase bindings without interpreting shell substitutions."""
    for _ in range(20):
        result = re.sub(r'\$\(([A-Z_][A-Z0-9_]*)\)',
                        lambda match: str(variables[match[1]]) if match[1] in variables else match[0], value)
        if result == value:
            require(not re.search(r'\$\(([A-Z_][A-Z0-9_]*)\)', result), f'unresolved pipeline binding: {result}')
            return result
        value = result
    raise Error('recursive pipeline bindings')


def toml_table(lines, header, values):
    """Write a flat, JSON-compatible TOML table used by the generated pack spec."""
    lines.append(header)
    for key, value in values.items():
        lines.append(f'{key} = {json.dumps(value)}')
    lines.append('')


def run(recipe_path):
    """Execute a reviewed local pipeline; no pipeline ever comes from an MDP archive."""
    recipe_path = Path(recipe_path).resolve()
    root = recipe_path.parent
    directory = root / ".mdp"
    directory.mkdir(exist_ok=True)
    with (directory / "pipeline.lock").open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("another producer pipeline is active in this project") from None
        return run_locked(recipe_path)


def run_locked(recipe_path):
    """Build and export one producer while its project lock is held."""
    root = recipe_path.parent
    recipe = json.loads(recipe_path.read_text())
    require(recipe.get('format') == 1, 'unsupported pipeline format')
    variables = {'PROJECT': str(root)}
    if os.environ.get('MDP_VARIABLES'):
        variables.update(json.loads(Path(os.environ['MDP_VARIABLES']).read_text()))
    for overlay in json.loads(os.environ.get('MDP_VARIABLE_OVERLAYS', '[]')):
        if Path(overlay).is_file():
            variables.update(json.loads(Path(overlay).read_text()))
    variables.update(recipe.get('bindings', {}))
    variables['PROJECT'] = str(root)
    environment = dict(os.environ, MMAKE_JOBS=os.environ.get('MDP_JOBS', '4'))
    for step in recipe.get('steps', []):
        command = expand(step, variables)
        print('[pipeline] ' + command, flush=True)
        subprocess.run(['bash', '-euo', 'pipefail', '-c', command], cwd=root, env=environment, check=True)
    if not recipe.get('artifact'):
        return
    require('package' in recipe, 'artifact pipeline requires a package identity')
    product = root / 'out/mdp/product'
    temporary = root / 'out/mdp/product.new'
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    records, links, destinations = [], [], set()
    def copy(source, area, destination, mode=None):
        relative(destination)
        identifier(area)
        identity = area + '/' + destination
        require(identity not in destinations, f'duplicate pipeline export: {identity}')
        destinations.add(identity)
        output = temporary / 'payload' / identity
        output.parent.mkdir(parents=True, exist_ok=True)
        require(source.is_file(), f'missing pipeline output: {source}')
        selected_mode = mode or (0o755 if source.stat().st_mode & 0o111 else 0o644)
        # Export staging is disposable. The immutable tar is still measured and
        # verified by pack(); linking unchanged-mode inputs avoids full SDK copies.
        try:
            require(source.stat().st_mode & 0o777 == selected_mode, 'copy required for mode normalization')
            os.link(source, output)
        except (OSError, Error):
            shutil.copyfile(source, output)
            output.chmod(selected_mode)
        records.append({'source': output.relative_to(temporary).as_posix(), 'area': area,
                        'destination': destination, 'mode': selected_mode})
    for export in recipe.get('exports', []):
        when = export.get('when', {})
        if any(str(variables.get(key)) != str(value) for key, value in when.items()):
            continue
        source = Path(expand(export['source'], variables))
        if not source.is_absolute():
            source = root / source
        if export.get('optional') and not source.exists():
            continue
        if source.is_dir():
            excluded = set(export.get('exclude', []))
            for file in sorted(source.rglob('*')):
                if (file.is_file() or file.is_symlink()) and not any(
                        fnmatch.fnmatch(part, pattern) for part in file.relative_to(source).parts for pattern in excluded):
                    prefix = export['destination'].rstrip('/')
                    destination = (prefix + '/' if prefix else '') + file.relative_to(source).as_posix()
                    if file.is_symlink() and export.get('preserve_links'):
                        links.append(dict(area=export['area'], destination=destination, target=os.readlink(file)))
                    else:
                        copy(file, export['area'], destination, export.get('mode'))
        else:
            copy(source, export['area'], export['destination'], export.get('mode'))
    if recipe.get('metadata') is not None:
        metadata = temporary / 'producer.json'
        metadata.write_text(json.dumps(recipe['metadata'], indent=2) + '\n')
        copy(metadata, 'metadata', recipe['package']['name'] + '.json')
        metadata.unlink()
    package = dict(recipe['package'])
    if (root / 'mdp.lock').is_file():
        sdk = json.loads((root / 'mdp.lock').read_text()).get('sdk')
        if sdk:
            package['sdk_id'] = sdk['id']
    for field, key in (('profile_sha256', 'MDP_PROFILE_FILE'), ('version_sha256', 'MDP_VERSION_FILE')):
        if environment.get(key):
            package[field] = digest(Path(environment[key]))
    dependencies = []
    receipt_file = environment.get('MDP_WORKSPACE')
    if receipt_file:
        receipt_file = Path(receipt_file) / '.mdp/builds.json'
        receipts = json.loads(receipt_file.read_text()) if receipt_file.exists() else {}
        manifest = __import__('tomllib').loads((root / 'mdp.toml').read_text()) if (root / 'mdp.toml').exists() else {}
        # The workspace passes the current variant's own dependency names.
        names = json.loads(environment.get('MDP_REQUIRES', '[]'))
        for name in names:
            receipt = receipts.get(name, {})
            if 'artifact' not in receipt:
                continue
            source = Path(environment['MDP_WORKSPACE']) / receipt['artifact']
            target = temporary / 'dependencies' / (name + '.tar.mdp')
            target.parent.mkdir(exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            dependencies.append(dict(name=receipt['package']['name'], kind='artifact',
                                     version=receipt['package']['version'], path=target.relative_to(temporary).as_posix(), sha256=receipt['sha256']))
    if environment.get('MDP_SDK_DESCRIPTOR'):
        context = json.loads(Path(environment['MDP_SDK_DESCRIPTOR']).read_text())
        existing = {d['name'] for d in dependencies}
        for artifact in context['artifacts']:
            name = artifact['package']['name']
            if name in existing:
                continue
            target = temporary / 'dependencies' / (name + '.tar.mdp')
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(artifact['path'], target)
            dependencies.append(dict(name=name,kind='artifact',version=artifact['package']['version'],
                                     path=target.relative_to(temporary).as_posix(),sha256=artifact['sha256']))
    lines = ['format = 1', '']
    toml_table(lines, '[package]', package)
    for dep in dependencies:
        toml_table(lines, '[[dependencies]]', dep)
    for record in records:
        toml_table(lines, '[[files]]', record)
    for link in links:
        toml_table(lines, '[[links]]', link)
    (temporary / 'mdp.toml').write_text('\n'.join(lines))
    lock_project(temporary / 'mdp.toml')
    if product.exists():
        shutil.rmtree(product)
    temporary.rename(product)
    pack(product / 'mdp.toml', root / relative(recipe['artifact']))
