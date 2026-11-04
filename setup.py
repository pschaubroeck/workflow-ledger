"""Compatibility entry point and skill bundler for the Python package pipeline."""

from pathlib import Path
import shutil

from setuptools.command.build_py import build_py
from setuptools import setup


ROOT = Path(__file__).resolve().parent
SKILL = Path(".agents/skills/workflow-ledger-operations")


class build_py_with_skill(build_py):
    """Bundle the canonical operations skill so installed setup can distribute it."""

    def run(self):
        super().run()
        source = ROOT / SKILL
        destination = Path(self.build_lib) / "workflow_ledger" / "_bundled" / "skills" / SKILL.name
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


setup(
    cmdclass={"build_py": build_py_with_skill},
    package_data={"workflow_ledger": ["_bundled/skills/workflow-ledger-operations/**/*"]},
)
