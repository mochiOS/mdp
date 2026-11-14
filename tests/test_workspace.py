"""Workspace dependency and SDK binding tests with real isolated Git projects."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mdp import workspace
from mdp.cli import Error, lock_project, pack, verify, assemble, composition_lock, sync, build
from mdp.pipeline import expand


class WorkspaceTests(unittest.TestCase):
    def test_real_workspace_pipeline_cache_and_artifact_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'seed').write_text('first')
            for name,requires in [('library',[]),('app',['library'])]:
                project=root/name;project.mkdir()
                subprocess.run(['git','init','-q',str(project)],check=True)
                recipe={'format':1,'steps':['mkdir -p out; printf actual > out/result; printf x >> out/counter'],
                        'package':dict(name=name,version='1',target='test',abi='test'),
                        'artifact':'out/'+name+'.tar.mdp','exports':[dict(source='out/result',area='files',destination=name)]}
                (project/'mdp.recipe.json').write_text(json.dumps(recipe))
                (project/'mdp.toml').write_text('format=1\n[build]\ncommand=["mdp","pipeline"]\nrequires='+json.dumps(requires)+'\ninputs='+json.dumps(['seed'] if name=='app' else [])+'\nartifact="out/'+name+'.tar.mdp"\n')
                subprocess.run(['git','-C',str(project),'add','.'],check=True)
                subprocess.run(['git','-C',str(project),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            cfg=root/'mdp.workspace.toml'
            cfg.write_text('format=1\ntarget="test"\nabi="test"\n[[projects]]\nname="library"\npath="library"\n[[projects]]\nname="app"\npath="app"\n')
            workspace.lock(cfg)
            workspace.build(cfg)
            with contextlib.redirect_stdout(io.StringIO()) as output:workspace.build(cfg)
            self.assertIn('[mdp cached] app',output.getvalue())
            self.assertEqual((root/'app/out/counter').read_bytes(),b'x')
            assemble(root/'mdp.assembly.lock',root/'assembled')
            self.assertEqual((root/'assembled/tree/files/app').read_bytes(),b'actual')
            self.assertEqual(len(verify(root/'app/out/app.tar.mdp')['dependencies']),1)
            (root/'seed').write_text('second')
            workspace.build(cfg)
            self.assertEqual((root/'app/out/counter').read_bytes(),b'xx')
            self.assertEqual((root/'library/out/counter').read_bytes(),b'x')

    def test_source_change_during_build_never_publishes_composition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir()
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'source').write_text('original')
            command=['python3','-c',"from pathlib import Path; Path('source').write_text('changed')"]
            (project/'mdp.toml').write_text('format=1\n[build]\ncommand='+json.dumps(command)+'\n')
            subprocess.run(['git','-C',str(project),'add','.'],check=True)
            subprocess.run(['git','-C',str(project),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            cfg=root/'mdp.workspace.toml';cfg.write_text('format=1\ntarget="test"\nabi="test"\n[[projects]]\nname="example"\npath="project"\n')
            workspace.lock(cfg)
            with self.assertRaisesRegex(Error,'source changed during'):workspace.build(cfg)
            self.assertFalse((root/'mdp.assembly.lock').exists())

    def test_workspace_environment_is_a_locked_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir()
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'mdp.toml').write_text('format=1\n[build]\ncommand=["true"]\n')
            subprocess.run(['git','-C',str(project),'add','.'],check=True)
            subprocess.run(['git','-C',str(project),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            cfg=root/'mdp.workspace.toml';cfg.write_text('format=1\ntarget="test"\nabi="test"\n[[projects]]\nname="example"\npath="project"\n')
            workspace.lock(cfg)
            cfg.write_text(cfg.read_text()+'\n[environment]\nCOMPILER="different"\n')
            with self.assertRaisesRegex(Error,'inputs changed'):workspace.build(cfg,dry_run=True)

    def test_area_filter_verifies_every_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'binary').write_bytes(b'system');(root/'sdk').write_bytes(b'sdk')
            project=root/'mdp.toml'
            project.write_text('''format=1
[package]
name="test"
version="1"
target="test"
abi="test"
[[files]]
source="binary"
area="system"
destination="bin/example"
mode=493
[[files]]
source="sdk"
area="sdk"
destination="lib/example.a"
mode=420
''')
            lock_project(project);pack(project,root/'test.tar.mdp')
            cfg=root/'mdp.assembly.toml';cfg.write_text('format=1\ntarget="test"\nabi="test"\n[[artifacts]]\npath="test.tar.mdp"\n')
            composition_lock(cfg);assemble(root/'mdp.assembly.lock',root/'assembled',{'system'})
            self.assertTrue((root/'assembled/tree/system/bin/example').is_file())
            self.assertFalse((root/'assembled/tree/sdk').exists())

    def test_cargo_diagnostics_are_retained_without_invalidating_resolution_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'source').write_text('original')
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            subprocess.run(['git','-C',str(root),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            lock=root/'Cargo.lock';lock.write_text('version=4\n[[package]]\nname="example"\nversion="1"\n')
            before=workspace.source_state(root)
            lock.write_text(lock.read_text()+'[[patch.unused]]\nname="unused"\nversion="1"\n')
            after=workspace.source_state(root)
            self.assertNotEqual(before['resolution'],after['resolution'])
            self.assertEqual(before['resolution_graph'],after['resolution_graph'])
            self.assertIn('unused',lock.read_text())

    def test_standalone_sdk_lock_sync_and_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'mdp.toml';context=root/'sdk.json'
            command=[sys.executable,'-c',"import os; from pathlib import Path; Path('result').write_text(os.environ['SDK_CHOICE'])"]
            project.write_text('format=1\n[build]\nsdk_context=true\ncommand='+json.dumps(command)+'\n')
            context.write_text(json.dumps({'format':1,'sdk':{'id':'a'*64,'target':'test','abi':'test'},'artifacts':[], 'sources':[], 'environment':{'SDK_CHOICE':'explicit'}}))
            with self.assertRaisesRegex(Error,'explicit SDK'):lock_project(project)
            lock_project(project,context)
            sync(project)
            with self.assertRaisesRegex(Error,'build --context'):build(project)
            build(project,context)
            self.assertEqual((root/'result').read_text(),'explicit')
            self.assertEqual(json.loads((root/'mdp.lock').read_text())['sdk']['id'],'a'*64)

    def test_nested_cargo_lock_changes_are_recorded_as_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);member=root/'driver';member.mkdir()
            subprocess.run(['git','init','-q',str(root)],check=True)
            lock=member/'Cargo.lock';lock.write_text('version=4\n[[package]]\nname="example"\nversion="1"\n')
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            subprocess.run(['git','-C',str(root),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            before=workspace.source_state(root)
            lock.write_text(lock.read_text().replace('version="1"','version="2"'))
            after=workspace.source_state(root)
            self.assertEqual(before['working_tree'],after['working_tree'])
            self.assertNotEqual(before['resolution_graph'],after['resolution_graph'])
            self.assertIn('driver/Cargo.lock',after['resolution'])
            self.assertIn('version="2"',lock.read_text())
            subprocess.run(['git','-C',str(root),'add','driver/Cargo.lock'],check=True)
            subprocess.run(['git','-C',str(root),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','resolution'],check=True)
            committed=workspace.source_state(root)
            self.assertNotEqual(after['revision'],committed['revision'])
            self.assertEqual(after['tracked_tree'],committed['tracked_tree'])
            self.assertEqual(after['working_tree'],committed['working_tree'])


    def test_committing_reviewed_working_content_keeps_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            subprocess.run(['git','init','-q',str(root)],check=True)
            file=root/'source';file.write_text('original')
            commit=['git','-C',str(root),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source']
            subprocess.run(['git','-C',str(root),'add','.'],check=True);subprocess.run(commit,check=True)
            file.write_text('reviewed change');(root/'module').write_text('new source')
            before=workspace.source_state(root)
            subprocess.run(['git','-C',str(root),'add','.'],check=True);subprocess.run(commit,check=True)
            after=workspace.source_state(root)
            self.assertNotEqual(before['revision'],after['revision'])
            self.assertEqual(before['content_sha256'],after['content_sha256'])
            file.write_text('different source')
            self.assertNotEqual(after['content_sha256'],workspace.source_state(root)['content_sha256'])

    def test_transitive_sources_include_shared_protocol_cycles(self):
        projects={'runtime':{'config':{'build':{'sources':['service']}}},
                  'service':{'config':{'build':{'sources':['runtime','viewkit']}}},
                  'viewkit':{'config':{'build':{}}}}
        self.assertEqual(workspace.source_closure(projects,'runtime'),{'runtime','service','viewkit'})
        projects['viewkit']['config']['build']['sources']=['missing']
        with self.assertRaisesRegex(Error,'unknown SDK source'):workspace.source_closure(projects,'runtime')

    def test_dependency_order_and_cycle(self):
        projects={'a':{'config':{'build':{'requires':['b']}}},'b':{'config':{'build':{}}}}
        self.assertEqual(workspace.order(projects),['b','a'])
        projects['b']['config']['build']['requires']=['a']
        with self.assertRaises(Error):workspace.order(projects)

    def test_bindings_preserve_real_shell_substitutions(self):
        self.assertEqual(expand('$(B) $(date +%s)',{'A':'/sdk','B':'$(A)/bin'}),'/sdk/bin $(date +%s)')
        with self.assertRaises(Error):expand('$(MISSING)',{})

    def test_source_lock_rejects_unreviewed_source_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir()
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'source').write_text('original')
            (project/'mdp.toml').write_text('format = 1\n[build]\ncommand = ["python3", "-c", "print(1)"]\n')
            subprocess.run(['git','-C',str(project),'add','.'],check=True)
            subprocess.run(['git','-C',str(project),'-c','commit.gpgsign=false','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],check=True)
            cfg=root/'mdp.workspace.toml';cfg.write_text('format = 1\ntarget="test"\nabi="test"\n[[projects]]\nname="example"\npath="project"\n')
            workspace.lock(cfg)
            workspace.build(cfg,dry_run=True)
            (project/'source').write_text('changed')
            with self.assertRaises(Error):workspace.build(cfg,dry_run=True)

    def test_verified_link_assembly_and_escape_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'binary').write_bytes(b'actual program')
            project=root/'mdp.toml'
            base='''format=1
[package]
name="test"
version="1"
target="test"
abi="test"
[[files]]
source="binary"
area="system"
destination="developer/bin/clang"
mode=493
[[links]]
area="system"
destination="bin/clang"
target="../developer/bin/clang"
'''
            project.write_text(base);lock_project(project);pack(project,root/'test.tar.mdp')
            self.assertEqual(verify(root/'test.tar.mdp')['format'],2)
            cfg=root/'mdp.assembly.toml';cfg.write_text('format=1\ntarget="test"\nabi="test"\n[[artifacts]]\npath="test.tar.mdp"\n')
            composition_lock(cfg);assemble(root/'mdp.assembly.lock',root/'assembled')
            alias=root/'assembled/tree/system/bin/clang'
            self.assertTrue(alias.is_symlink());self.assertEqual(alias.read_bytes(),b'actual program')
            project.write_text(base.replace('../developer/bin/clang','../../outside'))
            with self.assertRaises(Error):pack(project,root/'bad.tar.mdp')


if __name__=='__main__':unittest.main()
