from pathlib import Path
import subprocess
import tempfile
import unittest


class NativeOutgoingIdentity(unittest.TestCase):
    def test_constructor_observer_in_owned_process(self):
        root = Path(__file__).resolve().parents[1]
        compiler = Path('D:/mingw64/bin/gcc.exe')
        if not compiler.is_file():
            self.skipTest('native fixture compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / 'task-observer.exe'
            vendor=root / 'vendor/minhook'
            options={'capture_output':True,'timeout':30,
                     'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)}
            command=[str(compiler),'-std=c11','-O2','-Wall','-Wextra','-Werror=format',
                     '-I',str(vendor/'include'),str(root/'native/test_task_observer.c')]
            command.extend(str(vendor/p) for p in ('src/buffer.c','src/hook.c','src/trampoline.c','src/hde/hde64.c'))
            command.extend(['-o',str(program)])
            built=subprocess.run(command,**options)
            self.assertEqual(built.returncode,0,built.stderr.decode(errors='replace'))
            checked=subprocess.run([str(program)],**options)
            self.assertEqual(checked.returncode,0,checked.stderr.decode(errors='replace'))
            self.assertIn(b'passed',checked.stdout)

    def test_exact_input_mapping_in_owned_process(self):
        root = Path(__file__).resolve().parents[1]
        compiler = Path('D:/mingw64/bin/gcc.exe')
        if not compiler.is_file():
            self.skipTest('native fixture compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / 'outgoing-identity.exe'
            options = {'capture_output': True, 'timeout': 30,
                       'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
            built = subprocess.run([str(compiler), '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2',
                                    str(root / 'native/test_outgoing_identity.c'), '-o', str(program)], **options)
            self.assertEqual(built.returncode, 0, built.stderr.decode(errors='replace'))
            checked = subprocess.run([str(program)], **options)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors='replace'))
            self.assertIn(b'passed', checked.stdout)
