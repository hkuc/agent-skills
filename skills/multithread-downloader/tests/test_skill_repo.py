import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "init_skill_repo.py"


@unittest.skipUnless(shutil.which("git"), "git is required")
class SkillRepoTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_creates_installable_repo_with_seed_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "skills"
            result = self.run_script("--repo", str(repo), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["initialized_git"])
            self.assertEqual(payload["skill"], "multithread-downloader")
            self.assertTrue((repo / ".git").is_dir())
            self.assertTrue((repo / "skills/multithread-downloader/SKILL.md").is_file())
            self.assertIn("npx skills add", (repo / "README.md").read_text())
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo, text=True,
                stdout=subprocess.PIPE, check=True,
            )
            self.assertEqual(status.stdout, "")

    def test_empty_repo_has_gitkeep_and_can_be_initialized_again(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "skills"
            first = self.run_script("--repo", str(repo), "--empty-repo", "--json")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((repo / "skills/.gitkeep").is_file())
            second = self.run_script("--repo", str(repo), "--empty-repo", "--json")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(json.loads(second.stdout)["initialized_git"])
            self.assertIsNone(json.loads(second.stdout)["commit"])

    def test_existing_skill_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "skills"
            target = repo / "skills/multithread-downloader"
            target.mkdir(parents=True)
            marker = target / "SKILL.md"
            marker.write_text("keep me", encoding="utf-8")
            result = self.run_script("--repo", str(repo), "--json")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
