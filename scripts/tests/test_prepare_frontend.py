"""Verify isolated assembly preserves explicit overlays across repeated runs."""
import hashlib
from pathlib import Path
import runpy
import shutil
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]
prepare = runpy.run_path(str(ROOT / "packaging/integrated/prepare_frontend.py"))["prepare"]


class FrontendAssemblyTests(unittest.TestCase):
    def test_repeated_assembly_preserves_overlay_editors_and_single_api_adapter(self):
        overlay = ROOT / "integrated/frontend"
        with TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(ROOT / "web/src", target / "src")
            shutil.copyfile(ROOT / "web/index.html", target / "index.html")
            prepare(target, overlay)

            def digest():
                return {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in target.rglob("*") if path.is_file()}

            first = digest()
            prepare(target, overlay)
            self.assertEqual(first, digest(), "reassembly must not inject duplicate adapters or patches")
            for name in ("DomainListPage.tsx", "TtlRulesPage.tsx", "AdvancedConfigPage.tsx"):
                self.assertEqual((target / "src/pages" / name).read_bytes(),
                                 (overlay / "src/pages" / name).read_bytes())
            api = (target / "src/api.ts").read_text(encoding="utf-8")
            self.assertEqual(api.count("import { suiteFetch }"), 1)
            self.assertIn("return suiteFetch<T>(path, options)", api)


if __name__ == "__main__":
    unittest.main()
