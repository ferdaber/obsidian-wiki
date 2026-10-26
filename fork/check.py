#!/usr/bin/env python3
"""Static checks that gate every sync of the fork stack onto an upstream tag.

Usage: python3 fork/check.py [--base <upstream tag>]

Prints one line per result, ``PASS <id> <what>`` or ``FAIL <id> <detail>``.
Exit codes: 0 all checks pass, 1 any check fails, 2 usage or environment
error (not a git repo, unknown base, FORK.md missing without --base, the
CLI from this checkout cannot be run for C3).

Every check FAILs when its input is missing or empty; none passes vacuously.
C6 compares <base> with HEAD (committed state); uncommitted changes are not
seen by C6.

Standard library only; Python 3.9+.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

Line = Tuple[bool, str, str]

POINTER_LINE = (
    "> **Fork:** this is `ferdaber/obsidian-wiki`, a fork of "
    "`Ar9av/obsidian-wiki`. Read `FORK.md` before changing anything or "
    "syncing upstream."
)
BASE_RE = re.compile(r"^Upstream base: (\S+)$", re.MULTILINE)
REGISTER_HEADER = ["File", "Change", "Replaces upstream behavior", "Added"]
FORK_SECTIONS = [
    "## Purpose",
    None,  # the `Upstream base: <tag>` line
    "## Fork stack",
    "## Core skills",
    "## Patched upstream files",
    "## Install",
    "## Update",
    "## Syncing upstream",
    "## Uninstall",
    "## Never",
]
HOOK_SCRIPTS = ["wiki-session-recap.sh", "wiki-stop-capture.sh", "wiki-prompt-recall.sh"]
SECOND_LEVEL = ("memory", "hooks")
# `obsidian-wiki <word>` hits in the current tree that are prose, not commands:
# "the obsidian-wiki repo" (wiki-setup, wiki-update) and
# "no obsidian-wiki on PATH" (wiki-session-recap.sh).
PROSE_WORDS = frozenset({"repo", "on"})
# Uppercase words after `obsidian-wiki` are unverifiable (C3 FAILs them),
# except these exact upstream prose phrases in the hook scripts' injected
# HTML comments: "Injected by the obsidian-wiki UserPromptSubmit hook"
# (wiki-prompt-recall.sh) and "... SessionStart hook" (wiki-session-recap.sh).
PROSE_PHRASE_RE = re.compile(r"Injected by the obsidian-wiki (?:UserPromptSubmit|SessionStart) hook\b")
# The hook scripts call the CLI through a runner array
# (`runner=(obsidian-wiki)` or `runner=(python3 -m obsidian_wiki.cli)`, then
# `"${runner[@]}" memory recap ...`), so those spellings count as invocations.
# The match must be followed by whitespace; the words after it are then
# tokenized by `c3_scan_invocation`.
INVOKE_RE = re.compile(
    r"(?:(?<![^\s`(\"'<])obsidian-wiki"
    r"|\"\$\{runner\[@\]\}\""
    r"|\bpython3?\s+-m\s+obsidian_wiki(?:\.cli)?)"
    r"(?=\s)"
)
# A subcommand word to look up. Uppercase starts are handled separately
# (unverifiable); digits and `_` are looked up and so FAIL as unknown.
WORD_RE = re.compile(r"[a-z0-9_][A-Za-z0-9_-]*")
INSTALL_PREFIX_RE = re.compile(r"\b(?:un)?install\s+$")
PLUGIN_ROOT_RE = re.compile(r"\$(?:\{CLAUDE_PLUGIN_ROOT\}|CLAUDE_PLUGIN_ROOT\b)/([^\s\"'`;|&<>()]+)")
FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
SETUP_RE = re.compile(r"(?:obsidian-wiki|python3?\s+-m\s+obsidian_wiki(?:\.cli)?)\s+setup\b")
SHELL_OPERATOR_RE = re.compile(r"&&|\|\||;|\|")


class EnvError(Exception):
    """Usage or environment error: exit 2."""


# ---------------------------------------------------------------- helpers

def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


OUTSIDE, DELIM, INSIDE = 0, 1, 2


def fence_kinds(lines: List[str]) -> List[int]:
    """Per line: OUTSIDE, DELIM (a ``` or ~~~ fence line, any indentation) or
    INSIDE a fenced block. A fence closes only on a line of the same character
    at least as long as the opener with nothing else on it; an unclosed fence
    runs to the end of the file."""
    kinds: List[int] = []
    opener: Optional[str] = None
    for line in lines:
        if opener is None:
            m = FENCE_OPEN_RE.match(line)
            if m:
                opener = m.group(1)
                kinds.append(DELIM)
            else:
                kinds.append(OUTSIDE)
        else:
            s = line.strip()
            if s and set(s) == {opener[0]} and len(s) >= len(opener):
                opener = None
                kinds.append(DELIM)
            else:
                kinds.append(INSIDE)
    return kinds


def fence_mask(lines: List[str]) -> List[bool]:
    """True for each fence delimiter line or line inside a fenced block."""
    return [k != OUTSIDE for k in fence_kinds(lines)]


def unfenced_lines(text: str) -> List[Tuple[int, str]]:
    """(index, line) for each line outside fenced code blocks."""
    lines = text.splitlines()
    return [(i, l) for i, (l, f) in enumerate(zip(lines, fence_mask(lines))) if not f]


def section_lines(text: str, heading: str) -> Optional[List[str]]:
    """Lines after the exact heading line up to the next `## ` line, or None.
    Lines inside fenced code blocks are ignored: they neither match the
    heading, end the section, nor appear in the result."""
    lines = [l for _, l in unfenced_lines(text)]
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            out = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                out.append(nxt)
            return out
    return None


def parse_core_skills(fork_text: str) -> Tuple[List[str], List[str]]:
    """Return (names, problems) from FORK.md `## Core skills`."""
    lines = section_lines(fork_text, "## Core skills")
    if lines is None:
        return [], ["FORK.md has no `## Core skills` section"]
    names: List[str] = []
    problems: List[str] = []
    for line in lines:
        s = line.strip()
        if not s.startswith(("- ", "* ")):
            continue
        m = re.fullmatch(r"[-*]\s+`([^`\s]+)`", s)
        if not m:
            problems.append("malformed Core skills bullet: %r" % s)
            continue
        name = m.group(1)
        if name in names:
            problems.append("duplicate core skill: %s" % name)
        else:
            names.append(name)
    if not names:
        problems.append("FORK.md `## Core skills` lists no skills")
    return names, problems


def fork_base(root: Path) -> str:
    path = root / "FORK.md"
    if not path.is_file():
        raise EnvError("FORK.md not found at %s and --base not given" % path)
    found = [m.group(1) for _, l in unfenced_lines(read_text(path))
             for m in [BASE_RE.fullmatch(l.rstrip("\r"))] if m]
    if not found:
        raise EnvError("FORK.md has no `Upstream base: <tag>` line and --base not given")
    if len(found) > 1:
        raise EnvError("FORK.md has %d `Upstream base:` lines; expected one" % len(found))
    return found[0]


def plugin_skill_names(root: Path) -> Optional[List[str]]:
    d = root / "plugin-skills"
    if not d.is_dir():
        return None
    return sorted(os.listdir(d))


# ---------------------------------------------------------------- C1

def check_c1(root: Path) -> List[Line]:
    fork = root / "FORK.md"
    if not fork.is_file():
        return [(False, "C1", "FORK.md not found; cannot read Core skills")]
    names, problems = parse_core_skills(read_text(fork))
    out: List[Line] = [(False, "C1", p) for p in problems]
    if not names:
        return out
    entries = plugin_skill_names(root)
    if entries is None:
        return out + [(False, "C1", "plugin-skills/ directory not found")]
    want, have = set(names), set(entries)
    for n in sorted(want - have):
        out.append((False, "C1", "plugin-skills/ missing core skill: %s" % n))
    for n in sorted(have - want):
        out.append((False, "C1", "plugin-skills/ has extra entry not in Core skills: %s" % n))
    for n in sorted(want & have):
        link = root / "plugin-skills" / n
        if not os.path.islink(link):
            out.append((False, "C1", "plugin-skills/%s is not a symlink" % n))
            continue
        target = os.readlink(link)
        # The raw target must be exactly ../.skills/<name>: anything else
        # (absolute, chained through another symlink, redundant segments)
        # may resolve here but dangle once copied into the plugin cache.
        if target != "../.skills/%s" % n:
            out.append((False, "C1", "plugin-skills/%s symlink target is %r, expected '../.skills/%s'" % (n, target, n)))
            continue
        expected = root / ".skills" / n / "SKILL.md"
        got = link / "SKILL.md"
        if not expected.is_file():
            out.append((False, "C1", ".skills/%s/SKILL.md does not exist" % n))
            continue
        if not got.is_file() or got.resolve() != expected.resolve():
            out.append((False, "C1", "plugin-skills/%s/SKILL.md does not resolve to .skills/%s/SKILL.md (link -> %s)" % (n, n, target)))
    if not out:
        out.append((True, "C1", "plugin-skills/ holds exactly the %d core skills, each a symlink ../.skills/<name> resolving to .skills/<name>/SKILL.md" % len(names)))
    return out


# ---------------------------------------------------------------- C2

def check_c2(root: Path) -> List[Line]:
    path = root / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return [(False, "C2", ".claude-plugin/plugin.json not found")]
    try:
        data = json.loads(read_text(path))
    except ValueError as e:
        return [(False, "C2", ".claude-plugin/plugin.json is not valid JSON: %s" % e)]
    if not isinstance(data, dict) or "skills" not in data:
        return [(False, "C2", ".claude-plugin/plugin.json has no `skills` key")]
    if data["skills"] != "./plugin-skills/":
        return [(False, "C2", "plugin.json skills is %r, expected './plugin-skills/'" % (data["skills"],))]
    return [(True, "C2", "plugin.json skills is ./plugin-skills/")]


# ---------------------------------------------------------------- C3

def parse_help_choices(help_text: str) -> Set[str]:
    """Subcommand names from argparse --help: the `{a,b}` choices group of the
    positional-arguments section, plus the indented subcommand list (aliases
    shown as `name (alias)` included)."""
    m = re.search(
        r"^(?:positional arguments|positionals):[ \t]*\n(.*?)(?=^\S|\Z)",
        help_text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return set()
    body = m.group(1)
    names: Set[str] = set()
    brace = re.search(r"^[ \t]+\{([^}]*)\}", body, re.MULTILINE)
    if brace:
        for part in re.sub(r"\s+", "", brace.group(1)).split(","):
            if part:
                names.add(part)
    for lm in re.finditer(
        r"^ {4}([a-z][a-z0-9-]*)(?: \(([^)]*)\))?(?=\s|$)", body, re.MULTILINE
    ):
        names.add(lm.group(1))
        if lm.group(2):
            names.update(a.strip() for a in lm.group(2).split(",") if a.strip())
    return names


def parse_help_options(help_text: str) -> Dict[str, Optional[bool]]:
    """{option string: takes a value?} from argparse --help option lines
    (`  -h, --help`, `  --vault VAULT`, `  --mode {a,b}`). None means the
    option's arity cannot be told (optional or multiple values)."""
    opts: Dict[str, Optional[bool]] = {}
    for line in help_text.splitlines():
        m = re.match(r"^ {2}(-\S.*?)(?:\s{2,}|$)", line)
        if not m:
            continue
        for piece in m.group(1).split(", "):
            pm = re.fullmatch(r"(--?[A-Za-z0-9][A-Za-z0-9_-]*)(?:[ =](.*))?", piece.strip())
            if not pm:
                continue
            arg = pm.group(2)
            if arg is None:
                takes: Optional[bool] = False
            elif re.fullmatch(r"[^\s\[\]]+", arg) and "..." not in arg:
                takes = True
            else:
                takes = None
            opts[pm.group(1)] = takes
    return opts


def run_cli_help(root: Path, args: List[str]) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    attempts = [[sys.executable, "-m", "obsidian_wiki", *args, "--help"]]
    if shutil.which("uv"):
        attempts.append(["uv", "run", "--project", str(root), "--quiet",
                         "python", "-m", "obsidian_wiki", *args, "--help"])
    errors = []
    for cmd in attempts:
        try:
            p = subprocess.run(cmd, cwd=str(root), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append("%s: %s" % (" ".join(cmd[:3]), e))
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.decode("utf-8", "replace")
        tail = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()[-3:]
        errors.append("%s: exit %d: %s" % (" ".join(cmd[:3]), p.returncode, " | ".join(tail)))
    raise EnvError("cannot run the CLI from this checkout (obsidian-wiki %s--help): %s"
                   % ("".join(a + " " for a in args), "; ".join(errors)))


Options = Dict[str, Dict[str, Optional[bool]]]


def cli_spec(root: Path) -> Tuple[Dict[str, Set[str]], Options]:
    """Subcommands and options per level: ({"": top-level subcommands,
    "memory": ..., "hooks": ...}, {"": top-level options, "memory": ...}).
    Raises EnvError."""
    top_help = run_cli_help(root, [])
    top = parse_help_choices(top_help)
    if not top:
        raise EnvError("could not parse any subcommands from `obsidian-wiki --help`")
    commands = {"": top}
    options: Options = {"": parse_help_options(top_help)}
    for sub in SECOND_LEVEL:
        if sub not in top:
            commands[sub] = set()
            options[sub] = {}
            continue
        sub_help = run_cli_help(root, [sub])
        names = parse_help_choices(sub_help)
        if not names:
            raise EnvError("could not parse any actions from `obsidian-wiki %s --help`" % sub)
        commands[sub] = names
        options[sub] = parse_help_options(sub_help)
    return commands, options


def cli_commands(root: Path) -> Dict[str, Set[str]]:
    """{"": top-level subcommands, "memory": ..., "hooks": ...}. Raises EnvError."""
    return cli_spec(root)[0]


def logical_lines(text: str) -> List[Tuple[int, str]]:
    """(first line number, text) with backslash continuations joined."""
    lines = text.splitlines()
    out: List[Tuple[int, str]] = []
    i = 0
    while i < len(lines):
        start = i
        buf = lines[i].rstrip("\r")
        while buf.endswith("\\") and i + 1 < len(lines):
            i += 1
            buf = buf[:-1] + " " + lines[i].rstrip("\r")
        out.append((start + 1, buf))
        i += 1
    return out


# scan results
SKIP, OK, UNKNOWN, UNVERIFIABLE = "skip", "ok", "unknown", "unverifiable"


def scan_invocation(tokens: List[str], commands: Dict[str, Set[str]],
                    options: Options) -> Tuple[str, str]:
    """Classify the words after one invocation. Returns (result, words):
    SKIP (prose or a bare name mention), OK, UNKNOWN (a subcommand the CLI
    lacks) or UNVERIFIABLE (a variable, an uppercase word, an option the
    CLI's help does not list, or an option whose value cannot be skipped)."""
    consumed: List[str] = []
    level = ""
    i = 0
    while True:
        tok = tokens[i] if i < len(tokens) else ""
        shown = " ".join(consumed + [tok]).strip()
        if tok.startswith(("$", '"$', "'$")):
            return UNVERIFIABLE, shown
        if tok.startswith("-"):
            name = tok.split("=", 1)[0].rstrip(".,;:)")
            takes = options.get(level, {}).get(name, "missing")
            if takes == "missing" or takes is None:
                return UNVERIFIABLE, shown
            if "=" in tok:
                if not takes:
                    return UNVERIFIABLE, shown
                consumed.append(tok)
                i += 1
            elif takes:
                if i + 1 >= len(tokens):
                    return UNVERIFIABLE, shown
                consumed += [tok, tokens[i + 1]]
                i += 2
            else:
                consumed.append(tok)
                i += 1
            continue
        word_tok = tok
        if tok[:1] in ("\"", "'") and tok[1:2].isalpha():
            word_tok = tok[1:]  # a quoted literal word: check it too
        if word_tok[:1].isupper():
            return UNVERIFIABLE, shown
        m = WORD_RE.match(word_tok)
        if not m:
            # end of the command (punctuation, placeholder, end of line)
            return (SKIP if level == "" and not consumed else OK), shown
        word = m.group(0)
        whole = m.end() == len(word_tok)
        if level == "":
            if not consumed and word in PROSE_WORDS:
                return SKIP, shown
            if word not in commands.get("", set()):
                return UNKNOWN, " ".join(consumed + [word])
            if word in SECOND_LEVEL and whole:
                consumed.append(word)
                level = word
                i += 1
                continue
            return OK, shown
        if word not in commands.get(level, set()):
            return UNKNOWN, " ".join(consumed + [word])
        return OK, shown


def find_invocations(text: str, commands: Dict[str, Set[str]],
                     options: Options) -> List[Tuple[int, str, str]]:
    """(line number, result, words) for each `obsidian-wiki` invocation
    (also `"${runner[@]}"` and `python3 -m obsidian_wiki[.cli]`) that is not
    prose; backslash continuations are joined first."""
    found = []
    for lineno, line in logical_lines(text):
        exempt = {pm.start() + len("Injected by the ") for pm in PROSE_PHRASE_RE.finditer(line)}
        for m in INVOKE_RE.finditer(line):
            if m.start() in exempt:
                continue
            if INSTALL_PREFIX_RE.search(line[:m.start()]):
                continue
            rest = line[m.end():]
            tick = rest.find("`")
            if tick >= 0:
                rest = rest[:tick]  # a code span or command substitution ends here
            result, words = scan_invocation(rest.split(), commands, options)
            if result != SKIP:
                found.append((lineno, result, words))
    return found


def c3_files(root: Path) -> Tuple[List[Path], List[str]]:
    problems: List[str] = []
    files: List[Path] = []
    names: Set[str] = set(plugin_skill_names(root) or [])
    if not names:
        problems.append("plugin-skills/ missing or empty; no core skills to scan")
    fork = root / "FORK.md"
    if fork.is_file():
        names.update(parse_core_skills(read_text(fork))[0])
    for n in sorted(names):
        p = root / ".skills" / n / "SKILL.md"
        if p.is_file():
            files.append(p)
        else:
            problems.append(".skills/%s/SKILL.md not found" % n)
    hooks_dir = root / ".claude" / "hooks"
    for h in HOOK_SCRIPTS:
        if not (hooks_dir / h).is_file():
            problems.append(".claude/hooks/%s not found" % h)
    if hooks_dir.is_dir():
        files.extend(sorted(hooks_dir.glob("*.sh")))
    return files, problems


def check_c3(root: Path, commands: Optional[Dict[str, Set[str]]] = None,
             options: Optional[Options] = None) -> List[Line]:
    if commands is None or options is None:
        commands, options = cli_spec(root)  # may raise EnvError
    files, problems = c3_files(root)
    out: List[Line] = [(False, "C3", p) for p in problems]
    total = 0
    seen: Set[str] = set()
    for f in files:
        for _lineno, result, words in find_invocations(read_text(f), commands, options):
            total += 1
            if result == OK:
                continue
            if result == UNKNOWN:
                msg = "%s: obsidian-wiki %s" % (rel(root, f), words)
            else:
                msg = "%s: cannot verify obsidian-wiki %s" % (rel(root, f), words)
            if msg not in seen:
                seen.add(msg)
                out.append((False, "C3", msg))
    if total == 0:
        out.append((False, "C3", "found no `obsidian-wiki <subcommand>` calls in core skills or hook scripts"))
    if not out:
        out.append((True, "C3", "all %d obsidian-wiki calls in %d files exist in the CLI" % (total, len(files))))
    return out


# ---------------------------------------------------------------- C4

def collect_commands(node, acc: List) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "command":
                acc.append(v)
            else:
                collect_commands(v, acc)
    elif isinstance(node, list):
        for v in node:
            collect_commands(v, acc)


def check_c4(root: Path) -> List[Line]:
    path = root / "hooks" / "hooks.json"
    if not path.is_file():
        return [(False, "C4", "hooks/hooks.json not found")]
    try:
        data = json.loads(read_text(path))
    except ValueError as e:
        return [(False, "C4", "hooks/hooks.json is not valid JSON: %s" % e)]
    commands: List = []
    collect_commands(data, commands)
    if not commands:
        return [(False, "C4", "hooks/hooks.json has no `command` entries")]
    out: List[Line] = []
    n_paths = 0
    for cmd in commands:
        if not isinstance(cmd, str):
            out.append((False, "C4", "non-string command: %r" % (cmd,)))
            continue
        paths = PLUGIN_ROOT_RE.findall(cmd)
        if not paths:
            out.append((False, "C4", "command has no ${CLAUDE_PLUGIN_ROOT}/ script path: %s" % cmd))
            continue
        for p in paths:
            n_paths += 1
            if ".." in p.split("/"):
                out.append((False, "C4", "script path has a `..` segment: ${CLAUDE_PLUGIN_ROOT}/%s" % p))
                continue
            full = (root / p).resolve()
            try:
                full.relative_to(root.resolve())
            except ValueError:
                out.append((False, "C4", "script path escapes the repo: ${CLAUDE_PLUGIN_ROOT}/%s" % p))
                continue
            if not full.is_file():
                out.append((False, "C4", "script not found: ${CLAUDE_PLUGIN_ROOT}/%s" % p))
    if not out:
        out.append((True, "C4", "all %d script paths in hooks/hooks.json exist" % n_paths))
    return out


# ---------------------------------------------------------------- C5

def check_c5(root: Path) -> List[Line]:
    path = root / ".skills" / "wiki-setup" / "SKILL.md"
    if not path.is_file():
        return [(False, "C5", ".skills/wiki-setup/SKILL.md not found")]
    lines = read_text(path).splitlines()
    kinds = fence_kinds(lines)
    out: List[Line] = []
    checked = 0
    i = 0
    while i < len(lines):
        lineno = i + 1
        line = lines[i]
        if kinds[i] == DELIM:
            i += 1  # a fence delimiter line: no command on it
            continue
        in_fence = kinds[i] == INSIDE
        if in_fence:
            # join backslash continuations inside fenced blocks
            # (the backslash is dropped so `obsidian-wiki \<newline> setup` matches)
            while line.rstrip().endswith("\\") and i + 1 < len(lines) and kinds[i + 1] == INSIDE:
                i += 1
                line = line.rstrip()[:-1] + " " + lines[i]
        i += 1
        for m in SETUP_RE.finditer(line):
            pos, stop = m.start(), m.end()
            after = line[stop:stop + 1]
            if after and re.match(r"[A-Za-z0-9_-]", after):
                continue  # a different word, e.g. `obsidian-wiki setup-x`
            if after == "`":
                continue  # bare name mention: `obsidian-wiki setup`'s ...
            if in_fence:
                seg = line[pos:]
            elif line[:pos].count("`") % 2 == 1:
                end = line.find("`", stop)
                if end < 0:
                    out.append((False, "C5", "line %d: unterminated code span after `%s`" % (lineno, m.group(0))))
                    continue
                seg = line[pos:end]
            else:
                out.append((False, "C5", "line %d: `%s` outside a code span or block; cannot verify its flags" % (lineno, m.group(0))))
                continue
            # flags count only before the first shell operator after the match
            op = SHELL_OPERATOR_RE.search(seg, stop - pos)
            if op:
                seg = seg[:op.start()]
            checked += 1
            missing = [f for f in ("--project-only", "--no-hooks") if not re.search(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", seg)]
            if missing:
                out.append((False, "C5", "line %d: %r lacks %s" % (lineno, " ".join(seg.split()), " ".join(missing))))
    if checked == 0 and not out:
        out.append((False, "C5", "no `obsidian-wiki setup` invocations found in .skills/wiki-setup/SKILL.md"))
    if not out:
        out.append((True, "C5", "all %d `obsidian-wiki setup` invocations carry --project-only --no-hooks" % checked))
    return out


# ---------------------------------------------------------------- C6

def parse_register(fork_text: str) -> Tuple[Optional[List[str]], List[str]]:
    """Return (register paths or None if no table, problems)."""
    lines = section_lines(fork_text, "## Patched upstream files")
    if lines is None:
        return None, ["FORK.md has no `## Patched upstream files` section"]
    rows = [l.strip() for l in lines if l.strip().startswith("|")]
    if not rows:
        return None, ["`## Patched upstream files` has no table"]

    def cells(row: str) -> List[str]:
        body = row.strip()
        if body.startswith("|"):
            body = body[1:]
        if body.endswith("|"):
            body = body[:-1]
        return [c.strip() for c in body.split("|")]

    problems: List[str] = []
    if cells(rows[0]) != REGISTER_HEADER:
        problems.append("register header is %r, expected `| %s |`" % (rows[0], " | ".join(REGISTER_HEADER)))
        return None, problems
    if len(rows) < 2 or not re.fullmatch(r"\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*\|?", rows[1]):
        problems.append("register table has no `---` separator row")
        return None, problems
    paths: List[str] = []
    for row in rows[2:]:
        first = cells(row)[0]
        found = re.findall(r"`([^`]+)`", first)
        leftover = re.sub(r"`[^`]+`", "", first).replace(",", "").strip()
        if not found or leftover:
            problems.append("register row's File cell is not backticked path(s): %r" % first)
        paths.extend(p.strip() for p in found)
    return paths, problems


def check_c6(root: Path, base: str) -> List[Line]:
    fork = root / "FORK.md"
    if not fork.is_file():
        return [(False, "C6", "FORK.md not found; cannot read the patch register")]
    register, problems = parse_register(read_text(fork))
    out: List[Line] = [(False, "C6", p) for p in problems]
    if register is None:
        return out
    p = git(root, "-c", "core.quotepath=off", "diff", "--no-renames", "--name-status", "-z",
            "%s..HEAD" % base, "--")
    if p.returncode != 0:
        return out + [(False, "C6", "git diff %s..HEAD failed: %s" % (base, p.stderr.decode("utf-8", "replace").strip()))]
    fields = p.stdout.decode("utf-8", "surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        return out + [(False, "C6", "could not parse git diff --name-status output")]
    changed: List[Tuple[str, str]] = []
    for status, fpath in zip(fields[0::2], fields[1::2]):
        if status != "A":
            changed.append((status, fpath))
    reg = set(register)
    deleted = {fpath for status, fpath in changed if status == "D"}
    for status, fpath in changed:
        if fpath not in reg:
            out.append((False, "C6", "upstream file changed (%s) but not in the patch register: %s" % (status, fpath)))
    for r in register:
        if r in deleted:
            continue  # deleted by the fork stack: registered, and rightly absent
        if not os.path.lexists(root / r):
            out.append((False, "C6", "register file does not exist: %s" % r))
    if not out:
        out.append((True, "C6", "all %d upstream files changed since %s are in the patch register (%d entries), all present"
                    % (len(changed), base, len(register))))
    return out


# ---------------------------------------------------------------- C7

def pointer_problem(path: Path, name: str) -> Optional[str]:
    if not path.is_file():
        return "%s not found" % name
    lines = read_text(path).splitlines()
    fenced = fence_mask(lines)
    for i, line in enumerate(lines):
        # The title is the first `# ` line outside fenced code. The pointer
        # must then be the very next non-blank line (a fence opener there is
        # a non-pointer line and FAILs).
        if line.startswith("# ") and not fenced[i]:
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    if nxt.rstrip("\r") == POINTER_LINE:
                        return None
                    return "%s: first line under the title is not the FORK.md pointer line: %r" % (name, nxt)
            return "%s: nothing under the title line" % name
    return "%s has no `# ` title line" % name


def check_c7(root: Path) -> List[Line]:
    out: List[Line] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        prob = pointer_problem(root / name, name)
        if prob:
            out.append((False, "C7", prob))
    fork = root / "FORK.md"
    if not fork.is_file():
        out.append((False, "C7", "FORK.md not found"))
    else:
        lines = [(i, l.rstrip("\r")) for i, l in unfenced_lines(read_text(fork))]
        positions = []
        for sec in FORK_SECTIONS:
            label = sec or "Upstream base: <tag>"
            if sec is None:
                hits = [i for i, l in lines if BASE_RE.fullmatch(l)]
            else:
                hits = [i for i, l in lines if l == sec]
            if not hits:
                out.append((False, "C7", "FORK.md missing section: %s" % label))
            elif len(hits) > 1:
                out.append((False, "C7", "FORK.md has %d `%s` lines; expected one" % (len(hits), label)))
            else:
                positions.append((hits[0], label))
        for (a, la), (b, lb) in zip(positions, positions[1:]):
            if b < a:
                out.append((False, "C7", "FORK.md sections out of order: %s comes before %s" % (lb, la)))
    if not out:
        out.append((True, "C7", "pointer line under the title in CLAUDE.md and AGENTS.md; FORK.md has all ten sections in order"))
    return out


# ---------------------------------------------------------------- main

def run_check(cid: str, fn, *args) -> List[Line]:
    try:
        lines = fn(*args)
        if not lines:
            return [(False, cid, "check produced no result")]
        return lines
    except Exception as e:  # an unexpected error is a FAIL, never a crash
        return [(False, cid, "internal error: %s: %s" % (type(e).__name__, e))]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fork/check.py",
        description="Static checks on the fork stack (C1-C7). Exit 0 all pass, 1 any fail, 2 usage/environment error.",
    )
    parser.add_argument("--base", help="upstream tag the fork stack sits on (default: FORK.md `Upstream base:` line)")
    args = parser.parse_args(argv)

    root = repo_root()
    try:
        p = git(root, "rev-parse", "--show-toplevel")
        if p.returncode != 0:
            raise EnvError("%s is not in a git repository: %s" % (root, p.stderr.decode("utf-8", "replace").strip()))
        top = Path(p.stdout.decode("utf-8", "replace").strip()).resolve()
        if top != root.resolve():
            raise EnvError("fork/check.py must live in <repo>/fork/; git toplevel is %s, expected %s" % (top, root))
        base = args.base if args.base is not None else fork_base(root)
        if not base or git(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % base).returncode != 0:
            raise EnvError("unknown base %r: not a commit or tag in this repository; "
                           "fetch upstream tags: git fetch upstream --tags (or git fetch origin --tags)" % base)
        commands, options = cli_spec(root)
    except EnvError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except OSError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    results: List[Line] = []
    results += run_check("C1", check_c1, root)
    results += run_check("C2", check_c2, root)
    results += run_check("C3", check_c3, root, commands, options)
    results += run_check("C4", check_c4, root)
    results += run_check("C5", check_c5, root)
    results += run_check("C6", check_c6, root, base)
    results += run_check("C7", check_c7, root)

    for ok, cid, msg in results:
        print("%s %s %s" % ("PASS" if ok else "FAIL", cid, msg))
    return 0 if results and all(ok for ok, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
