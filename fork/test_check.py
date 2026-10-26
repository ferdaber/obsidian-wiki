"""Black-box tests for fork/check.py, written from the fork spec.

Each test builds a throwaway git repository that copies ``fork/check.py`` to
``<tmp>/fork/check.py`` next to a minimal fork layout, mutates it, runs the
checker as a subprocess from an unrelated working directory, and asserts on
the ``PASS <id>`` / ``FAIL <id>`` line prefixes and the exit code.

Run from the repo root with either:
    python3 -m unittest fork/test_check.py -v
    python3 fork/test_check.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE / "check.py"

IDS = ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]

POINTER = (
    "> **Fork:** this is `ferdaber/obsidian-wiki`, a fork of `Ar9av/obsidian-wiki`. "
    "Read `FORK.md` before changing anything or syncing upstream."
)

CORE = ["alpha", "beta", "wiki-setup"]

REGISTER = [
    "CLAUDE.md",
    "AGENTS.md",
    ".claude-plugin/plugin.json",
    ".skills/wiki-setup/SKILL.md",
]

HOOK_SCRIPTS = ["wiki-session-recap.sh", "wiki-stop-capture.sh", "wiki-prompt-recall.sh"]

FAKE_CLI = '''\
import argparse
import sys


def main(argv=None):
    p = argparse.ArgumentParser(prog="obsidian-wiki", description="fake cli")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("setup", help="install skills")
    s.add_argument("--vault")
    s.add_argument("--project-only", action="store_true")
    s.add_argument("--no-hooks", action="store_true")
    s.add_argument("--timezone")
    sub.add_parser("context-pack", help="build a context pack")
    m = sub.add_parser("memory", help="memory files")
    msub = m.add_subparsers(dest="memcmd")
    ms = msub.add_parser("sync", help="sync memory")
    ms.add_argument("--verb")
    ms.add_argument("--field", action="append")
    msub.add_parser("now", help="print a timestamp")
    h = sub.add_parser("hooks", help="agent hooks")
    hsub = h.add_subparsers(dest="hookcmd")
    hsub.add_parser("install", help="install hooks")
    p.parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

PLUGIN_JSON = '{\n  "name": "obsidian-wiki",\n  "skills": "%s"\n}\n'

HOOKS_JSON_TMPL = """{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "bash \\"${CLAUDE_PLUGIN_ROOT}/.claude/hooks/%s\\"" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "bash \\"${CLAUDE_PLUGIN_ROOT}/.claude/hooks/%s\\"" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "bash \\"${CLAUDE_PLUGIN_ROOT}/.claude/hooks/%s\\"" }] }]
  }
}
"""

ALPHA_SKILL = """---
name: alpha
---
# Alpha

After writing, run `obsidian-wiki memory sync --verb INGEST --field pages=3`.
"""

BETA_SKILL = """---
name: beta
---
# Beta

Get a stamp:

```bash
obsidian-wiki memory now --date
obsidian-wiki context-pack --budget 2000
```
"""

# Non-core skill: must be ignored by C3 even though it names a bogus command.
OTHER_SKILL = """---
name: other
---
# Other

Run `obsidian-wiki bogus-command` (not a core skill, so C3 ignores it).
"""

SETUP_SKILL_UPSTREAM = """---
name: wiki-setup
---
# Wiki Setup

Run `obsidian-wiki setup --vault ~/brain`.

```bash
obsidian-wiki setup --vault "$VAULT" --timezone UTC
```
"""

SETUP_SKILL_FORK = """---
name: wiki-setup
---
# Wiki Setup

Run `obsidian-wiki setup --vault ~/brain --project-only --no-hooks`.

```bash
obsidian-wiki setup --vault "$VAULT" --timezone UTC --no-hooks --project-only
```

Note: `obsidian-wiki setup`'s defaults are described below.
"""

HOOK_BODY = """#!/usr/bin/env bash
set -euo pipefail
stamp="$(obsidian-wiki memory now)"
echo "$stamp"
"""

HOOK_BODY_INSTALL = """#!/usr/bin/env bash
# re-register with: obsidian-wiki hooks install
obsidian-wiki memory sync --verb STOP
"""

SECTION_ORDER = [
    "purpose",
    "base",
    "stack",
    "core",
    "register",
    "install",
    "update",
    "syncing",
    "uninstall",
    "never",
]


def fork_md(base="v0.0.2", core=None, register=None, order=None):
    core = CORE if core is None else core
    register = REGISTER if register is None else register
    order = SECTION_ORDER if order is None else order
    core_lines = "".join("- `%s`\n" % n for n in core)
    reg_lines = "".join(
        "| `%s` | patched | upstream default | 2026-10-26 |\n" % f for f in register
    )
    sections = {
        "purpose": "## Purpose\n\nPlugin plus CLI over one local vault per machine.\n",
        "base": "Upstream base: %s\n" % base,
        "stack": "## Fork stack\n\n- `fork: add FORK.md and pointers`\n",
        "core": "## Core skills\n\n" + core_lines,
        "register": (
            "## Patched upstream files\n\n"
            "| File | Change | Replaces upstream behavior | Added |\n"
            "|---|---|---|---|\n" + reg_lines
        ),
        "install": "## Install\n\nClone the fork and install the plugin.\n",
        "update": "## Update\n\nFetch and reset to origin/main.\n",
        "syncing": "## Syncing upstream\n\nUse the sync-upstream skill.\n",
        "uninstall": "## Uninstall\n\nUninstall the plugin and the CLI.\n",
        "never": "## Never\n\n- Edit `.github/`.\n",
    }
    return "# Fork notes\n\n" + "\n".join(sections[k] for k in order)


def doc_with_pointer(title, pointer=True):
    body = "Upstream body text.\n\n## Section\n\nMore text.\n"
    if pointer:
        return "# %s\n\n%s\n\n%s" % (title, POINTER, body)
    return "# %s\n\n%s" % (title, body)


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def clean_env(ceiling=None):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            del env[k]
    if ceiling:
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    env.pop("PYTHONPATH", None)
    return env


def git(root, *args):
    r = subprocess.run(GIT + ["-C", str(root)] + list(args), capture_output=True,
                       text=True, env=clean_env())
    if r.returncode != 0:
        raise RuntimeError("git %s failed: %s%s" % (args, r.stdout, r.stderr))
    return r.stdout


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_template(root):
    """Build the passing fixture repo in ``root``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    # --- upstream base v0.0.1 ---
    write(root, "README.md", "# Upstream\n\nReadme.\n")
    write(root, "docs/guide.md", "# Guide\n")
    write(root, "docs/extra.md", "# Extra\n")
    write(root, "CLAUDE.md", doc_with_pointer("Agent Context", pointer=False))
    write(root, "AGENTS.md", doc_with_pointer("Agent Context", pointer=False))
    write(root, ".claude-plugin/plugin.json", PLUGIN_JSON % "./.skills/")
    write(root, ".skills/alpha/SKILL.md", ALPHA_SKILL)
    write(root, ".skills/beta/SKILL.md", BETA_SKILL)
    write(root, ".skills/other/SKILL.md", OTHER_SKILL)
    write(root, ".skills/wiki-setup/SKILL.md", SETUP_SKILL_UPSTREAM)
    write(root, ".claude/hooks/wiki-session-recap.sh", HOOK_BODY)
    write(root, ".claude/hooks/wiki-stop-capture.sh", HOOK_BODY_INSTALL)
    write(root, ".claude/hooks/wiki-prompt-recall.sh", HOOK_BODY)
    write(root, "obsidian_wiki/__init__.py", "")
    write(root, "obsidian_wiki/__main__.py", FAKE_CLI)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "upstream base")
    git(root, "tag", "v0.0.1")
    # --- upstream v0.0.2: README changes upstream ---
    write(root, "README.md", "# Upstream\n\nReadme, revised upstream.\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "upstream release")
    git(root, "tag", "v0.0.2")
    # --- fork stack ---
    write(root, "FORK.md", fork_md())
    write(root, "CLAUDE.md", doc_with_pointer("Agent Context"))
    write(root, "AGENTS.md", doc_with_pointer("Agent Context"))
    write(root, ".claude-plugin/plugin.json", PLUGIN_JSON % "./plugin-skills/")
    (root / "plugin-skills").mkdir()
    for n in CORE:
        os.symlink("../.skills/%s" % n, root / "plugin-skills" / n)
    write(root, "hooks/hooks.json", HOOKS_JSON_TMPL % tuple(HOOK_SCRIPTS))
    write(root, ".skills/wiki-setup/SKILL.md", SETUP_SKILL_FORK)
    (root / "fork").mkdir()
    shutil.copy2(CHECK, root / "fork" / "check.py")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fork: stack")


def parse(stdout):
    """Return {id: [statuses]} from PASS/FAIL lines."""
    out = {}
    for line in stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] in ("PASS", "FAIL"):
            out.setdefault(parts[1], []).append(parts[0])
    return out


class CheckTestBase(unittest.TestCase):
    tmpl_dir = None
    tmpl = None

    @classmethod
    def setUpClass(cls):
        if not CHECK.exists():
            raise RuntimeError("fork/check.py does not exist yet")
        cls.tmpl_dir = tempfile.mkdtemp(prefix="forkcheck-tmpl-")
        cls.tmpl = Path(cls.tmpl_dir) / "repo"
        build_template(cls.tmpl)

    @classmethod
    def tearDownClass(cls):
        if cls.tmpl_dir:
            shutil.rmtree(cls.tmpl_dir, ignore_errors=True)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="forkcheck-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.root = Path(self._tmp) / "repo"
        shutil.copytree(self.tmpl, self.root, symlinks=True)
        # Run from an unrelated directory: check.py must find the root from its own path.
        self.cwd = Path(self._tmp) / "elsewhere"
        self.cwd.mkdir()

    # --- helpers ---
    def write(self, rel, text):
        write(self.root, rel, text)

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def commit(self, msg="fork: change"):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", msg)

    def set_fork_md(self, **kw):
        self.write("FORK.md", fork_md(**kw))

    def run_check(self, *args):
        r = subprocess.run(
            [sys.executable, str(self.root / "fork" / "check.py")] + list(args),
            cwd=str(self.cwd), capture_output=True, text=True, timeout=120,
            env=clean_env(ceiling=self._tmp),
        )
        return r

    def describe(self, r):
        return "exit=%s\n--- stdout ---\n%s--- stderr ---\n%s" % (r.returncode, r.stdout, r.stderr)

    def assertAllPass(self, r):
        res = parse(r.stdout)
        self.assertEqual(r.returncode, 0, self.describe(r))
        self.assertEqual(sorted(res), IDS, self.describe(r))
        for i in IDS:
            self.assertEqual(res[i], ["PASS"], "%s\n%s" % (i, self.describe(r)))

    def assertOnlyFails(self, r, failing):
        """``failing`` ids FAIL (no PASS line); every other id has exactly one PASS; exit 1."""
        res = parse(r.stdout)
        msg = self.describe(r)
        self.assertEqual(r.returncode, 1, msg)
        self.assertEqual(sorted(res), IDS, msg)
        for i in IDS:
            if i == failing:
                self.assertTrue(res[i], msg)
                self.assertTrue(all(s == "FAIL" for s in res[i]), "%s\n%s" % (i, msg))
            else:
                self.assertEqual(res[i], ["PASS"], "%s should pass\n%s" % (i, msg))

    def assertFails(self, r, cid):
        res = parse(r.stdout)
        msg = self.describe(r)
        self.assertEqual(r.returncode, 1, msg)
        self.assertIn("FAIL", res.get(cid, []), msg)


class TestGeneral(CheckTestBase):
    def test_check_script_present(self):
        self.assertTrue(CHECK.exists(), "fork/check.py does not exist")

    def test_passing_fixture_with_explicit_base(self):
        self.assertAllPass(self.run_check("--base", "v0.0.2"))

    def test_base_defaults_from_fork_md(self):
        self.assertAllPass(self.run_check())

    def test_explicit_base_overrides_fork_md(self):
        # Against v0.0.1, README.md (changed upstream in v0.0.2, not in register)
        # shows as modified, so C6 fails only when --base really is used.
        self.assertOnlyFails(self.run_check("--base", "v0.0.1"), "C6")

    def test_default_base_follows_fork_md_line(self):
        self.set_fork_md(base="v0.0.1")
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")

    def test_unknown_base_tag_exit_2(self):
        r = self.run_check("--base", "nosuchtag")
        self.assertEqual(r.returncode, 2, self.describe(r))

    def test_unknown_base_from_fork_md_exit_2(self):
        self.set_fork_md(base="nosuchtag")
        self.commit()
        r = self.run_check()
        self.assertEqual(r.returncode, 2, self.describe(r))

    def test_fork_md_missing_without_base_exit_2(self):
        (self.root / "FORK.md").unlink()
        self.commit()
        r = self.run_check()
        self.assertEqual(r.returncode, 2, self.describe(r))

    def test_not_a_git_repo_exit_2(self):
        shutil.rmtree(self.root / ".git")
        r = self.run_check("--base", "v0.0.2")
        self.assertEqual(r.returncode, 2, self.describe(r))

    def test_unknown_option_exit_2(self):
        r = self.run_check("--no-such-option")
        self.assertEqual(r.returncode, 2, self.describe(r))

    def test_output_lines_only_pass_or_fail(self):
        r = self.run_check("--base", "v0.0.2")
        for line in r.stdout.splitlines():
            if line.strip():
                self.assertRegex(line, r"^(PASS|FAIL) C[1-7]( |$)", self.describe(r))


class TestC1PluginSkills(CheckTestBase):
    def test_extra_symlink_fails(self):
        os.symlink("../.skills/other", self.root / "plugin-skills" / "other")
        self.commit()
        # C3 scans what the plugin ships (plugin-skills/), so the extra skill's
        # bogus command may fail C3 too; C1 is the check that must catch it.
        self.assertFails(self.run_check(), "C1")

    def test_missing_symlink_fails(self):
        (self.root / "plugin-skills" / "beta").unlink()
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_symlink_to_nonexistent_skill_fails(self):
        os.symlink("../.skills/gamma", self.root / "plugin-skills" / "gamma")
        self.set_fork_md(core=CORE + ["gamma"])
        self.commit()
        # C3 may also fail here (a core SKILL.md it must scan is missing); the
        # spec does not settle that, so only C1 is asserted.
        self.assertFails(self.run_check(), "C1")

    def test_absolute_symlink_fails(self):
        link = self.root / "plugin-skills" / "beta"
        link.unlink()
        os.symlink(str(self.root / ".skills" / "beta"), link)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_copied_directory_instead_of_symlink_fails(self):
        link = self.root / "plugin-skills" / "beta"
        link.unlink()
        shutil.copytree(self.root / ".skills" / "beta", link)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_symlink_to_wrong_skill_fails(self):
        # plugin-skills/beta must resolve to .skills/beta, not some other skill.
        link = self.root / "plugin-skills" / "beta"
        link.unlink()
        os.symlink("../.skills/other", link)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_empty_core_list_fails(self):
        self.set_fork_md(core=[])
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_empty_core_list_and_no_plugin_skills_is_not_vacuous_pass(self):
        shutil.rmtree(self.root / "plugin-skills")
        self.set_fork_md(core=[])
        self.commit()
        # Whether C3 also fails with nothing to scan is not settled by the spec.
        self.assertFails(self.run_check(), "C1")

    def test_core_list_ends_at_next_section(self):
        # Bullets after the next "## " heading are not core skills.
        text = self.read("FORK.md").replace(
            "## Install\n\n", "## Install\n\n- `other`\n\n"
        )
        self.write("FORK.md", text)
        self.commit()
        self.assertAllPass(self.run_check())


class TestC2PluginJson(CheckTestBase):
    def test_skills_points_at_dot_skills_fails(self):
        self.write(".claude-plugin/plugin.json", PLUGIN_JSON % "./.skills/")
        self.commit()
        self.assertOnlyFails(self.run_check(), "C2")

    def test_skills_without_trailing_slash_fails(self):
        self.write(".claude-plugin/plugin.json", PLUGIN_JSON % "./plugin-skills")
        self.commit()
        self.assertOnlyFails(self.run_check(), "C2")


class TestC3CliReferences(CheckTestBase):
    def _patch_alpha(self, extra):
        self.write(".skills/alpha/SKILL.md", ALPHA_SKILL + "\n" + extra + "\n")
        self.set_fork_md(register=REGISTER + [".skills/alpha/SKILL.md"])
        self.commit()

    def test_unknown_top_level_subcommand_fails(self):
        self._patch_alpha("Then run `obsidian-wiki bogus --flag`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_unknown_memory_subcommand_fails(self):
        self._patch_alpha("Then run `obsidian-wiki memory bogus`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_unknown_hooks_subcommand_fails(self):
        self._patch_alpha("Then run `obsidian-wiki hooks bogus`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_hyphenated_prefix_is_not_a_known_subcommand(self):
        # "context" alone is not a subcommand of the fake CLI; "context-pack" is.
        self._patch_alpha("Then run `obsidian-wiki context --x`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_unknown_subcommand_in_hook_script_fails(self):
        self.write(".claude/hooks/wiki-prompt-recall.sh", HOOK_BODY + "obsidian-wiki bogus\n")
        self.set_fork_md(register=REGISTER + [".claude/hooks/wiki-prompt-recall.sh"])
        self.commit()
        self.assertOnlyFails(self.run_check(), "C3")

    def test_non_core_skill_is_not_scanned(self):
        # .skills/other/SKILL.md names `obsidian-wiki bogus-command` in the fixture.
        self.assertIn("obsidian-wiki bogus-command", self.read(".skills/other/SKILL.md"))
        self.assertAllPass(self.run_check())

    def test_uses_cli_from_checkout(self):
        # Adding a subcommand to the checkout's CLI makes a reference to it valid.
        self.write("obsidian_wiki/__main__.py", FAKE_CLI.replace(
            'sub.add_parser("context-pack", help="build a context pack")',
            'sub.add_parser("context-pack", help="build a context pack")\n'
            '    sub.add_parser("brandnew", help="new command")'))
        self._patch_alpha("Then run `obsidian-wiki brandnew`.")
        # obsidian_wiki/__main__.py is an upstream file too; register it.
        self.set_fork_md(register=REGISTER + [".skills/alpha/SKILL.md",
                                              "obsidian_wiki/__main__.py"])
        self.commit()
        self.assertAllPass(self.run_check())


class TestC4HooksJson(CheckTestBase):
    def test_missing_script_fails(self):
        self.write("hooks/hooks.json", HOOKS_JSON_TMPL % (
            HOOK_SCRIPTS[0], "wiki-missing.sh", HOOK_SCRIPTS[2]))
        self.commit()
        self.assertOnlyFails(self.run_check(), "C4")

    def test_no_commands_fails(self):
        self.write("hooks/hooks.json", '{\n  "hooks": {}\n}\n')
        self.commit()
        self.assertOnlyFails(self.run_check(), "C4")


class TestC5WikiSetup(CheckTestBase):
    def _setup_skill(self, text):
        self.write(".skills/wiki-setup/SKILL.md", text)
        self.commit()

    def test_inline_setup_without_flags_fails(self):
        self._setup_skill(SETUP_SKILL_FORK + "\nAlso run `obsidian-wiki setup --vault x`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_inline_setup_missing_one_flag_fails(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x --project-only`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_fenced_setup_without_flags_fails(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\n```bash\nobsidian-wiki setup --vault x --no-hooks\n```\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_in_separate_code_span_do_not_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nRun `obsidian-wiki setup --vault x` with "
                          "`--project-only --no-hooks`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_bare_mention_followed_by_backtick_is_skipped(self):
        # Fixture already contains "`obsidian-wiki setup`'s"; passing fixture proves skip.
        self.assertIn("`obsidian-wiki setup`'s", self.read(".skills/wiki-setup/SKILL.md"))
        self.assertAllPass(self.run_check())

    def test_only_bare_mention_fails_as_zero_occurrences(self):
        self._setup_skill("---\nname: wiki-setup\n---\n# Wiki Setup\n\n"
                          "Note: `obsidian-wiki setup`'s defaults are described below.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_no_setup_occurrences_fails(self):
        self._setup_skill("---\nname: wiki-setup\n---\n# Wiki Setup\n\nNothing here.\n")
        self.assertOnlyFails(self.run_check(), "C5")


class TestC6Register(CheckTestBase):
    def test_modified_upstream_file_not_in_register_fails(self):
        self.write("docs/guide.md", "# Guide\n\nForked.\n")
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")

    def test_modified_upstream_file_in_register_passes(self):
        self.write("docs/guide.md", "# Guide\n\nForked.\n")
        self.set_fork_md(register=REGISTER + ["docs/guide.md"])
        self.commit()
        self.assertAllPass(self.run_check())

    def test_deleted_upstream_file_not_in_register_fails(self):
        (self.root / "docs" / "extra.md").unlink()
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")

    def test_renamed_upstream_file_not_in_register_fails(self):
        git(self.root, "mv", "docs/extra.md", "docs/renamed.md")
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")

    def test_added_file_not_in_register_passes(self):
        self.write("docs/new-fork-page.md", "# New\n")
        self.commit()
        self.assertAllPass(self.run_check())

    def test_register_file_that_does_not_exist_fails(self):
        self.set_fork_md(register=REGISTER + ["docs/gone.md"])
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")

    def test_uncommitted_modification_is_ignored(self):
        # C6 checks committed state only (<base>..HEAD).
        self.write("docs/guide.md", "# Guide\n\nUncommitted edit.\n")
        self.assertAllPass(self.run_check())


class TestC7Pointers(CheckTestBase):
    def test_pointer_missing_in_agents_md_fails(self):
        self.write("AGENTS.md", doc_with_pointer("Agent Context", pointer=False))
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_pointer_missing_in_claude_md_fails(self):
        self.write("CLAUDE.md", doc_with_pointer("Agent Context", pointer=False))
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_pointer_not_first_line_under_title_fails(self):
        self.write("AGENTS.md", "# Agent Context\n\nSome intro first.\n\n%s\n\nBody.\n" % POINTER)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_pointer_text_altered_fails(self):
        self.write("AGENTS.md", "# Agent Context\n\n%s\n\nBody.\n"
                   % POINTER.replace("Read `FORK.md`", "Read FORK.md"))
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_pointer_directly_after_title_without_blank_line_passes(self):
        self.write("AGENTS.md", "# Agent Context\n%s\n\nBody.\n" % POINTER)
        self.commit()
        self.assertAllPass(self.run_check())

    def test_fork_md_missing_never_section_fails(self):
        self.set_fork_md(order=[s for s in SECTION_ORDER if s != "never"])
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_fork_md_sections_out_of_order_fails(self):
        order = list(SECTION_ORDER)
        i, j = order.index("update"), order.index("install")
        order[i], order[j] = order[j], order[i]
        self.set_fork_md(order=order)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")

    def test_fork_md_missing_upstream_base_line_with_explicit_base_fails(self):
        self.set_fork_md(order=[s for s in SECTION_ORDER if s != "base"])
        self.commit()
        self.assertOnlyFails(self.run_check("--base", "v0.0.2"), "C7")



# --- Tests for the second round of rules (C1, C3, C4, C5, C6, C7 hardening) ---


class TestC3UnverifiableWords(CheckTestBase):
    def _patch_alpha(self, extra):
        self.write(".skills/alpha/SKILL.md", ALPHA_SKILL + "\n" + extra + "\n")
        self.set_fork_md(register=REGISTER + [".skills/alpha/SKILL.md"])
        self.commit()

    def test_dollar_variable_after_memory_fails(self):
        self._patch_alpha("Then run `obsidian-wiki memory $VERB`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_quoted_dollar_variable_after_memory_fails(self):
        self._patch_alpha('Then run `obsidian-wiki memory "${VERB}"`.')
        self.assertOnlyFails(self.run_check(), "C3")

    def test_dollar_variable_as_top_level_subcommand_fails(self):
        self._patch_alpha("Then run `obsidian-wiki $CMD --x`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_dollar_variable_after_hooks_fails(self):
        self._patch_alpha("Then run `obsidian-wiki hooks $ACTION`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_uppercase_word_fails(self):
        self._patch_alpha("Then run `obsidian-wiki Memory_sync`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_unknown_option_after_memory_fails(self):
        self._patch_alpha("Then run `obsidian-wiki memory --bogus-option`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_unknown_option_at_top_level_fails(self):
        self._patch_alpha("Then run `obsidian-wiki --bogus-option`.")
        self.assertOnlyFails(self.run_check(), "C3")

    def test_backslash_continuation_with_bogus_word_fails(self):
        self._patch_alpha("```bash\nobsidian-wiki memory \\\n  bogus --verb X\n```")
        self.assertIn("memory \\\n", self.read(".skills/alpha/SKILL.md"))
        self.assertOnlyFails(self.run_check(), "C3")

    def test_backslash_continuation_with_valid_word_passes(self):
        self._patch_alpha("```bash\nobsidian-wiki memory \\\n  sync --verb X\n```")
        self.assertIn("memory \\\n", self.read(".skills/alpha/SKILL.md"))
        self.assertAllPass(self.run_check())

    def test_variable_in_later_argument_passes(self):
        # Only the word after obsidian-wiki / memory / hooks is checked.
        self._patch_alpha('Then run `obsidian-wiki memory sync --verb "$VERB"`.')
        self.assertAllPass(self.run_check())


class TestC5SetupForms(CheckTestBase):
    def _setup_skill(self, text):
        self.write(".skills/wiki-setup/SKILL.md", text)
        self.commit()

    def test_python3_module_cli_setup_without_flags_fails(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `python3 -m obsidian_wiki.cli setup --vault x`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_python_module_setup_without_flags_fails(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `python -m obsidian_wiki setup --vault x`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_python3_module_cli_setup_with_flags_passes(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `python3 -m obsidian_wiki.cli setup --vault x "
                          "--project-only --no-hooks`.\n")
        self.assertAllPass(self.run_check())

    def test_several_spaces_before_setup_without_flags_fails(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki   setup --vault x`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_after_and_and_do_not_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x && echo "
                          "--project-only --no-hooks`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_after_or_or_do_not_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x || echo "
                          "--project-only --no-hooks`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_after_semicolon_do_not_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x; echo "
                          "--project-only --no-hooks`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_after_pipe_do_not_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x | tee "
                          "--project-only --no-hooks`.\n")
        self.assertOnlyFails(self.run_check(), "C5")

    def test_flags_before_and_and_count(self):
        self._setup_skill(SETUP_SKILL_FORK +
                          "\nAlso run `obsidian-wiki setup --vault x --project-only "
                          "--no-hooks && echo done`.\n")
        self.assertAllPass(self.run_check())


class TestC7NeverInCodeFence(CheckTestBase):
    def test_never_heading_only_inside_fence_fails(self):
        text = fork_md(order=[s for s in SECTION_ORDER if s != "never"])
        text += "\n```markdown\n## Never\n\n- Edit `.github/`.\n```\n"
        self.write("FORK.md", text)
        self.commit()
        self.assertOnlyFails(self.run_check(), "C7")


class TestC1RawSymlinkTarget(CheckTestBase):
    def test_target_through_chained_symlink_fails(self):
        os.symlink(".skills", self.root / "chain")
        link = self.root / "plugin-skills" / "beta"
        link.unlink()
        os.symlink("../chain/beta", link)
        # Sanity: it still resolves to the right SKILL.md.
        self.assertEqual((link / "SKILL.md").resolve(),
                         (self.root / ".skills" / "beta" / "SKILL.md").resolve())
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")

    def test_target_with_redundant_segments_fails(self):
        link = self.root / "plugin-skills" / "beta"
        link.unlink()
        os.symlink("../.skills/../.skills/beta", link)
        self.assertTrue((link / "SKILL.md").exists())
        self.commit()
        self.assertOnlyFails(self.run_check(), "C1")


class TestC4DotDotPath(CheckTestBase):
    def test_path_escaping_plugin_root_fails(self):
        # ${CLAUDE_PLUGIN_ROOT}/../<repo-dir-name>/.claude/hooks/x.sh resolves to the
        # real script but contains "..", which C4 must reject.
        name = self.root.name
        tmpl = HOOKS_JSON_TMPL.replace("${CLAUDE_PLUGIN_ROOT}/.claude/hooks/%s",
                                       "${CLAUDE_PLUGIN_ROOT}/%s", 1)
        rel = "../%s/.claude/hooks/%s" % (name, HOOK_SCRIPTS[0])
        self.write("hooks/hooks.json", tmpl % (rel, HOOK_SCRIPTS[1], HOOK_SCRIPTS[2]))
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/" + rel, self.read("hooks/hooks.json"))
        self.assertTrue((self.root / rel).exists())
        self.commit()
        self.assertOnlyFails(self.run_check(), "C4")


class TestC6DeletedFiles(CheckTestBase):
    def test_deleted_upstream_file_in_register_passes(self):
        (self.root / "docs" / "extra.md").unlink()
        self.set_fork_md(register=REGISTER + ["docs/extra.md"])
        self.commit()
        self.assertAllPass(self.run_check())

    def test_deleted_file_missing_from_register_still_fails(self):
        (self.root / "docs" / "extra.md").unlink()
        (self.root / "docs" / "guide.md").unlink()
        self.set_fork_md(register=REGISTER + ["docs/extra.md"])
        self.commit()
        self.assertOnlyFails(self.run_check(), "C6")


class TestBaseExitCode(CheckTestBase):
    def test_unknown_base_still_exits_2(self):
        r = self.run_check("--base", "v9.9.9-missing")
        self.assertEqual(r.returncode, 2, self.describe(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
