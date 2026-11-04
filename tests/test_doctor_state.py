import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import node


class DoctorStateTests(unittest.TestCase):
    def check_state(self, state):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'state.json').write_text(json.dumps(state), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = node.main(['doctor', '--root', temp])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())['status'], 'failed')

    def test_unknown_profile(self):
        self.check_state({'profile': 'nonsense', 'modules': {}})

    def test_missing_required_modules(self):
        self.check_state({'profile': 'inference', 'modules': {}})

    def test_external_path(self):
        self.check_state({'profile': 'inference', 'modules': {'korax': {'path': '../outside'}}})

    def test_unknown_runtime_and_unpinned_ref(self):
        for change in ({'runtime': 'typo'}, {'ref': 'main'}):
            manifest = node.load_manifest()
            manifest['modules']['korax'].update(change)
            with self.assertRaises(ValueError):
                node.validate_manifest(manifest)


if __name__ == '__main__':
    unittest.main()
