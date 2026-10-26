# ferdaber/obsidian-wiki fork

## Purpose

This fork runs obsidian-wiki as a Claude Code plugin plus the `obsidian-wiki` CLI, over one local vault per machine (`~/brain`). It uses 14 of upstream's 40 skills (the core skills below), the 3 upstream hook scripts shipped through the plugin, and the upstream CLI for locked memory writes. It does not use the other skills, named vaults (`wiki-switch`), or `obsidian-wiki hooks install`. Upstream is `Ar9av/obsidian-wiki`; the fork only adds the changes listed here.

Upstream base: v2026.10.3

## Fork stack

The fork stack is these `fork:` commits on top of the upstream base, in order:

1. `fork: add FORK.md and pointers` - this file, plus the pointer line in `CLAUDE.md` and `AGENTS.md`.
2. `fork: curate plugin skills` - `plugin.json` points at `plugin-skills/`, which holds the 14 core skills.
3. `fork: ship hooks through the plugin` - `hooks/hooks.json` registers the 3 upstream hook scripts.
4. `fork: keep wiki-setup from global installs` - `wiki-setup` runs `setup` with `--project-only --no-hooks`.
5. `fork: add static checks` - `fork/check.py`.
6. `fork: add sync-upstream skill` - `.claude/skills/sync-upstream/SKILL.md`.

Later fork changes are appended as new `fork:` commits. Each sync ends with an annotated tag `fork-<upstream tag>` (for example `fork-v2026.10.3`) on the new `main`, and origin also carries upstream's `v*` tags so fresh clones have the base. Fork tags use `fork-`, not `fork/`: the CLI's version comes from `git describe`, and hatch-vcs cannot parse `fork/v2026.10.3`. The stack is `git log --reverse <Upstream base>..main`.

## Core skills

- `llm-wiki`
- `wiki-setup`
- `wiki-ingest`
- `wiki-capture`
- `wiki-update`
- `wiki-query`
- `wiki-context-pack`
- `wiki-status`
- `wiki-lint`
- `cross-linker`
- `tag-taxonomy`
- `wiki-dedup`
- `wiki-rebuild`
- `code-understand`

## Patched upstream files

Every change to an upstream file is listed here, one file per row.

| File | Change | Replaces upstream behavior | Added |
|---|---|---|---|
| `AGENTS.md` | Pointer line to `FORK.md` as the first line under the title | Upstream has no fork pointer; the line sends agents to this file first | fork-v2026.10.3 |
| `CLAUDE.md` | None of its own: upstream ships it as a symlink to `AGENTS.md`, so it carries the pointer through the link. Keep it a symlink | Same as `AGENTS.md` | fork-v2026.10.3 |
| `.claude-plugin/plugin.json` | `skills` set to `./plugin-skills/` | The plugin listed all 40 skills from `./.skills/` | fork-v2026.10.3 |
| `.skills/wiki-setup/SKILL.md` | Every `obsidian-wiki setup` invocation carries `--project-only --no-hooks` | `setup` symlinked all 40 skills into every agent's skills dir and registered hooks in `~/.claude/settings.json`, duplicating the plugin's | fork-v2026.10.3 |

## Install

Variables used below:

```bash
FORK_CLONE=~/src/obsidian-wiki
VAULT=~/brain
```

1. Make a full clone of the fork at `$FORK_CLONE`. Do not use a shallow clone: the CLI takes its version from git metadata and breaks. On the work machine, clone however the work machine allows.

```bash
git clone https://github.com/ferdaber/obsidian-wiki.git $FORK_CLONE
```

2. Add the clone as a local marketplace and install the plugin.

```bash
claude plugin marketplace add $FORK_CLONE
claude plugin install obsidian-wiki@obsidian-wiki
```

3. Install the CLI from the same clone.

```bash
uv tool install $FORK_CLONE
uv tool update-shell
```

4. Set up the vault.

First home machine:

```bash
obsidian-wiki setup --vault ~/brain --project-only --no-hooks --timezone America/New_York --remote https://github.com/ferdaber/brain.git
git -C ~/brain branch -M main   # setup's git init may name the branch master
obsidian-wiki sync              # first commit and push, so other machines can clone it
```

`setup` runs `git init` and adds `origin` but pushes nothing. Its summary prints "Skills installed: 40"; with `--project-only` that is only a count, and nothing is installed into `~/.claude/skills/`.

Other home machines: clone `ferdaber/brain` to `~/brain`, then run the same `setup` command.

```bash
git clone https://github.com/ferdaber/brain.git ~/brain
obsidian-wiki setup --vault ~/brain --project-only --no-hooks --timezone America/New_York --remote https://github.com/ferdaber/brain.git
```

Work machine (local git only, no remote; if `setup` asks "Set up GitHub sync?", answer N):

```bash
obsidian-wiki setup --vault ~/brain --project-only --no-hooks --timezone America/New_York
git -C ~/brain init   # only if it is not already a repo
```

5. Open a new Claude Code session. The recap appears.

Per-prompt recall is on by default. Set `WIKI_PROMPT_RECALL=false` in the machine's environment to turn it off. If the recap is silent, set `WIKI_RECAP_DEBUG=1` to debug it.

## Update

Run on each machine:

```bash
FORK_CLONE=~/src/obsidian-wiki
git -C $FORK_CLONE fetch origin --tags
git -C $FORK_CLONE reset --hard origin/main
claude plugin marketplace update obsidian-wiki
claude plugin update obsidian-wiki@obsidian-wiki
uv tool install --reinstall $FORK_CLONE
```

Then restart Claude Code.

## Syncing upstream

Sync when a new upstream `v*` tag appears and you want it. Run the `sync-upstream` skill (`.claude/skills/sync-upstream/SKILL.md`) in a Claude Code session in a fork clone with push rights: it prepares the sync and a sync report, Ferdy approves the report, then it finalizes. Never run a sync on the work machine.

Rollback, then run Update on each machine:

```bash
git fetch origin --tags
PREV=fork-<previous tag>
git push --force-with-lease=main:"$(git rev-parse origin/main)" origin "$PREV^{}:refs/heads/main"
```

## Uninstall

```bash
claude plugin uninstall obsidian-wiki@obsidian-wiki
claude plugin marketplace remove obsidian-wiki
uv tool uninstall obsidian-wiki
```

The vault and its git history stay.

## Never

- Run `obsidian-wiki setup` without `--project-only --no-hooks`.
- Run `obsidian-wiki hooks install`.
- Edit `.github/`.
- Run a sync on the work machine.
