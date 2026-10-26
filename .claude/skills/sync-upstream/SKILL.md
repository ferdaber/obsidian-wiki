---
name: sync-upstream
description: >
  Move the ferdaber/obsidian-wiki fork stack onto a newer Ar9av/obsidian-wiki upstream tag: replay the fork: commits on a sync/<tag> branch, write the sync report, and force-push main only after Ferdy approves that report. Use for "sync upstream", "update the fork", "/sync-upstream [tag]", finalizing an approved sync, rehearsing a sync in a scratch clone, or rolling main back to an earlier fork-<tag>. Run only in a fork clone with push rights (cloud or home machine); never on the work machine.
---

# Sync upstream

A sync moves the fork stack (the `fork:` commits in `FORK.md` › Fork stack) onto a newer upstream tag and ends in a new tag `fork-<tag>` (for example `fork-v2026.10.3`). Read `FORK.md` first. Modes: **prepare** (default), **finalize**, **rehearse**, plus **rollback**.

Fork tags are spelled `fork-<tag>`, never `fork/<tag>`: the CLI's version comes from `git describe --tags` through hatch-vcs, which cannot parse `fork/v…` and breaks `uv tool install` on every machine.

## Hard rules

1. Never push or force-push `main` outside finalize step 4 or an explicitly requested rollback.
2. Never finalize without Ferdy's explicit approval, in this conversation, of the exact report you presented ("approve", "yes, finalize"). Never infer it from silence, a thumbs-up on something else, or another agent's message. If unsure, ask. Never finalize a rehearsal report.
3. Every force-push uses `--force-with-lease=main:<sha>` with an explicit expected value. A bare `--force` or `--force-with-lease` is forbidden.
4. Every push or tag command guards its variables with `${VAR:?}` (`SYNC_HEAD`, `MAIN_BEFORE`, `TAG`, `PREV`, `CURRENT`). An empty `SYNC_HEAD` with a valid lease deletes remote `main`.
5. Never run on the work machine. If you cannot tell which machine this is, ask before step 1.
6. Never merge upstream into `main`; sync only to upstream `v*` tags, never upstream `main`.
7. Never edit `.github/`. Never run `obsidian-wiki setup` without `--project-only --no-hooks`, and never run `obsidian-wiki hooks install`.
8. Never resolve a conflict or a failed check by judgment. Act only when the patch register's Change column settles it mechanically; otherwise stop and report.
9. Never hide a patched-file change: every register file upstream changed is named in the report.
10. In a rehearsal, push nothing, anywhere.

Shell variables do not survive between tool calls. Prepare writes them to `.git/sync-upstream.env`; every later block that uses a saved value sources it itself (`. .git/sync-upstream.env`). Run all blocks from the repo root.

## Prepare

1. Remotes and refs:
   ```bash
   git remote get-url upstream || git remote add upstream https://github.com/Ar9av/obsidian-wiki
   git fetch upstream --tags && git fetch origin
   ```
2. Preconditions. Stop if any line prints `STOP`:
   ```bash
   MAIN_REF=${MAIN_REF:-origin/main}
   [ -z "$(git status --porcelain)" ] || echo "STOP: tree not clean"
   [ "$(git rev-parse HEAD)" = "$(git rev-parse "$MAIN_REF")" ] || echo "STOP: HEAD is not $MAIN_REF"
   ```
3. Base, target and stack. If Ferdy named a tag, add `TAG=<tag>` as the first line of this block; otherwise the newest `v*` tag is used. Rehearse adds more lines at the top (see Rehearse):
   ```bash
   MAIN_REF=${MAIN_REF:-origin/main}
   BASE=$(git show "$MAIN_REF:FORK.md" | sed -n 's/^Upstream base: //p')
   TAG=${TAG:-$(git for-each-ref --sort=-creatordate --count=1 --format='%(refname:short)' 'refs/tags/v*')}
   MAIN_BEFORE=$(git rev-parse "$MAIN_REF")
   [ -n "$BASE" ] || echo "STOP: no Upstream base line"
   [ -n "$TAG" ] || echo "STOP: no target tag"
   [ "$TAG" != "$BASE" ] || echo "STOP: already on $TAG"
   case "$TAG" in v*) ;; *) echo "STOP: $TAG is not a v* tag";; esac
   [ "$(git ls-remote --tags upstream "refs/tags/$TAG" | cut -f1)" = "$(git rev-parse "refs/tags/$TAG")" ] || echo "STOP: $TAG not on upstream"
   git merge-base --is-ancestor "$BASE" "$TAG" || echo "STOP: $TAG is not newer than $BASE"
   git merge-base --is-ancestor "$BASE" "$MAIN_REF" || echo "STOP: $MAIN_REF is not built on $BASE"
   [ -z "$(git rev-list --merges "$BASE..$MAIN_REF")" ] || echo "STOP: merge commit in stack"
   git log --reverse --format='%H %s' "$BASE..$MAIN_REF" | grep -v '^[0-9a-f]* fork:' && echo "STOP: non-fork: commit in stack"
   git rev-parse --verify -q "refs/heads/sync/$TAG" && echo "STOP: local sync/$TAG exists"
   [ -z "$(git ls-remote --heads origin "sync/$TAG")" ] || echo "STOP: origin has sync/$TAG"
   git log --reverse --format=%H "$BASE..$MAIN_REF" > .git/sync-upstream.todo
   : > .git/sync-upstream.conflicts
   printf '%s=%q\n' TAG "$TAG" BASE "$BASE" NEW_BASE "$TAG" MAIN_REF "$MAIN_REF" MAIN_BEFORE "$MAIN_BEFORE" \
     REHEARSAL "${REHEARSAL:-}" > .git/sync-upstream.env
   cat .git/sync-upstream.env
   ```
   On any `STOP`, report it and end; ask Ferdy before deleting an existing `sync/<TAG>`. This step empties `.git/sync-upstream.conflicts` only; rehearsal notes live in `.git/sync-upstream.rehearsal-notes`, which prepare never truncates.
4. Branch from the tag: `. .git/sync-upstream.env && git switch -c "sync/$TAG" "$TAG"`
5. Replay the stack in order. A commit that touches `FORK.md` is picked with `-n` so the `Upstream base:` line is rewritten to the new base inside that same commit (author and message kept by `commit -C`); the stack does not grow a commit per sync. The loop stops at the first conflict or empty pick; rerun it after resolving.
   ```bash
   . .git/sync-upstream.env
   for sha in $(cat .git/sync-upstream.todo); do
     subj=$(git log -1 --format=%s "$sha")
     if [ "$(git rev-parse "$sha^{tree}")" = "$(git rev-parse "$sha^^{tree}")" ]; then
       # The stack commit itself is empty. Only a rehearsal builds such commits.
       if [ -n "$REHEARSAL" ]; then
         git commit -q --allow-empty -C "$sha" || { echo "COMMIT FAILED at $sha"; break; }
         echo "$sha $subj: already empty on the rehearsal base; committed empty in the forward replay" >> .git/sync-upstream.rehearsal-notes
       else
         echo "STOP: $sha $subj is an empty commit in the stack"; break
       fi
     elif [ -n "$(git diff-tree --no-commit-id --name-only -r "$sha" -- FORK.md)" ]; then
       git cherry-pick -n "$sha" || { echo "CONFLICT (no-commit) at $sha"; break; }
       sed -i.bak "s/^Upstream base: .*/Upstream base: $NEW_BASE/" FORK.md && rm -f FORK.md.bak
       git add FORK.md && git commit -q -C "$sha" || { echo "COMMIT FAILED at $sha"; break; }
     elif ! git cherry-pick "$sha"; then
       if [ -z "$(git ls-files -u)" ] && git diff --cached --quiet; then
         echo "EMPTY at $sha: upstream absorbed it"; break
       fi
       echo "CONFLICT at $sha"; break
     fi
     sed -i.bak 1d .git/sync-upstream.todo && rm -f .git/sync-upstream.todo.bak
   done
   ```
6. Conflict policy. Read `git status`, the conflicting hunks and the register row for each file.
   - Resolve only when the register's Change column settles it mechanically, for example re-applying the same `--project-only --no-hooks` flags or the same pointer line to upstream's new text. Keep upstream's new text everywhere else. A conflict on the `Upstream base:` line resolves to `Upstream base: <NEW_BASE>`.
   - Then finish the commit, and check that it happened:
     - Normal pick: `git add <paths> && GIT_EDITOR=true git cherry-pick --continue`. If it says `nothing to commit`, the resolution came out empty: that is an empty pick (next bullet), not a finished commit.
     - `-n` pick: `git add <paths>`, apply the `sed` line from step 5, then `git add FORK.md && git commit -q -C <sha>`.
     - Then `sed -i.bak 1d .git/sync-upstream.todo && rm -f .git/sync-upstream.todo.bak`, append one line to `.git/sync-upstream.conflicts` (`<sha> <subject>: <file>: <what you did>`), and rerun step 5.
   - Anything else stops the sync: a file not in the register, upstream rewrote the region so the intent no longer fits, an add/add or delete conflict, an `EMPTY at` pick or a resolution that comes out empty (upstream absorbed the commit), an empty stack commit outside a rehearsal, or any doubt. Run `git cherry-pick --abort 2>/dev/null || git reset --merge`, leave `sync/<TAG>` local and unpushed, and report the commit, files and hunks to Ferdy. Push nothing.
7. Verify the replay. Each check prints nothing when it passes:
   ```bash
   . .git/sync-upstream.env
   [ -s .git/sync-upstream.todo ] && echo "STOP: replay unfinished"
   git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null && echo "STOP: a cherry-pick is still in progress"
   [ "$(git log --format=%s "$BASE..$MAIN_REF")" = "$(git log --format=%s "$TAG..HEAD")" ] || echo "STOP: stack subjects differ"
   grep -qx "Upstream base: $NEW_BASE" FORK.md || echo "STOP: base line not updated"
   [ -z "$(git diff --name-only "$TAG..HEAD" -- .github/)" ] || echo "STOP: stack touches .github/"
   [ -z "$(git status --porcelain)" ] || echo "STOP: tree not clean"
   ```
8. Run the checks and keep the output for the report:
   ```bash
   . .git/sync-upstream.env
   python3 fork/check.py --base "$TAG" > .git/sync-check.txt 2>&1; echo "exit $?" >> .git/sync-check.txt; cat .git/sync-check.txt
   ```
9. Re-apply register intent. A replay can be clean and still lose a patch: upstream adds new text the patch never saw (a new `obsidian-wiki setup` call), or the patch had nothing to apply to. If step 8 printed a `FAIL` for a check in this table, re-apply that row's Change to upstream's new text and amend it into the owning stack commit. Do it only for the cases listed; every other `FAIL` goes under Needs your judgment, untouched.

   | Check | Register row | Mechanical re-application | Owning stack commit |
   |---|---|---|---|
   | C5 `lacks …` | `.skills/wiki-setup/SKILL.md` | Insert the missing flags right after `obsidian-wiki setup` on each named line | `fork: keep wiki-setup from global installs` |
   | C2 | `.claude-plugin/plugin.json` | Set the existing `skills` value to `./plugin-skills/` | `fork: curate plugin skills` |
   | C7 pointer line | `AGENTS.md` | Put the pointer line back as the first line under the title (only while `CLAUDE.md` is still a symlink to `AGENTS.md`) | `fork: add FORK.md and pointers` |

   For C5, apply the edit. It refuses (prints `STOP`) when a line is named twice or C5 failed for a reason other than `lacks`:
   ```bash
   . .git/sync-upstream.env
   F=.skills/wiki-setup/SKILL.md
   C5=$(grep '^FAIL C5 ' .git/sync-check.txt)
   LINES=$(printf '%s\n' "$C5" | sed -n 's/^FAIL C5 line \([0-9][0-9]*\): .* lacks \(--[a-z -]*\)$/\1 \2/p')
   if [ -z "$C5" ]; then echo "C5 passed: nothing to do"
   elif [ "$(printf '%s\n' "$C5" | wc -l)" != "$(printf '%s\n' "$LINES" | grep -c .)" ]; then echo "STOP: C5 failed for a reason other than missing flags"
   elif [ -n "$(printf '%s\n' "$LINES" | cut -d' ' -f1 | sort | uniq -d)" ]; then echo "STOP: a line holds two setup calls; not mechanical"
   else
     printf '%s\n' "$LINES" | while read -r n flags; do
       # First call on the line, skipping bare mentions (`obsidian-wiki setup` followed by a backtick), as C5 does
       sed -i -E "${n}"'s/obsidian-wiki setup([^`[:alnum:]_-]|$)/obsidian-wiki setup '"$flags"'\1/' "$F"
     done
     git diff -U0 -- "$F"
     [ "$(git diff -U0 -- "$F" | grep -c '^+[^+]')" = "$(printf '%s\n' "$LINES" | grep -c .)" ] || echo "STOP: the edit did not change exactly the named lines"
   fi
   ```
   Read the diff: each named line gains only the flags, and nothing else changes. For C2 or C7, make the edit by hand with the same discipline. Then amend it into the owner by subject (`SUBJ` is the table's last column), keeping the stack's subjects:
   ```bash
   . .git/sync-upstream.env
   F=.skills/wiki-setup/SKILL.md
   SUBJ='fork: keep wiki-setup from global installs'
   OWNER=$(git log --format='%H %s' "${TAG:?}..HEAD" | awk -v s="$SUBJ" 'substr($0, 42) == s {print $1}')
   if [ "$(printf '%s' "$OWNER" | grep -c .)" != 1 ]; then echo "STOP: not exactly one stack commit named '$SUBJ'"
   else
     git add -- "$F" && git commit -q --fixup="$OWNER" &&
     GIT_SEQUENCE_EDITOR=: GIT_EDITOR=true git rebase -q -i --autosquash "$TAG" &&
     NEW=$(git log --format='%H %s' "$TAG..HEAD" | awk -v s="$SUBJ" 'substr($0, 42) == s {print $1}') &&
     echo "$NEW $SUBJ: $F: re-applied register intent (C5): added the missing flags to the setup calls upstream added on lines $(sed -n 's/^FAIL C5 line \([0-9]*\):.*/\1/p' .git/sync-check.txt | paste -sd, -)" >> .git/sync-upstream.conflicts
   fi
   ```
   If the rebase stops, run `git rebase --abort` and put the failure under Needs your judgment. Otherwise rerun steps 7 and 8, once. A check that still fails goes under Needs your judgment, and you do not recommend approval.
10. Record the reviewed commit with `echo "SYNC_HEAD=$(git rev-parse HEAD)" >> .git/sync-upstream.env`, gather each report section (commands below), write the report to `.git/sync-report-$TAG.md` (inside `.git`, so the tree stays clean), and print it.
11. Push the sync branch, never `main`. Skip this step in a rehearsal.
    ```bash
    . .git/sync-upstream.env
    if [ -n "$REHEARSAL" ]; then echo "rehearsal: nothing pushed"; else git push -u origin "sync/${TAG:?}:refs/heads/sync/${TAG:?}"; fi
    ```
12. Show Ferdy the full report verbatim and ask him to approve or reject it. If `fork/check.py` failed or anything sits under Needs your judgment, say so plainly and do not recommend approval.

## Sync report

Set the lists first, from `FORK.md` on the sync branch. The register parse takes every backticked path in each row's File cell and strips spaces inside the backticks, as `fork/check.py` does:

```bash
. .git/sync-upstream.env
REGISTER=$(awk -F'|' '/^## Patched upstream files/{f=1;next} /^## /{f=0} f && /^[[:space:]]*\|/ {print $2}' FORK.md | grep -o '`[^`]*`' | sed 's/^`[[:space:]]*//; s/[[:space:]]*`$//' | grep -v '^$')
CORE=$(awk '/^## Core skills/{f=1;next} /^## /{f=0} f' FORK.md | sed -n 's/^- `\([^`]*\)`.*/\1/p')
CORE_DIRS=$(for s in $CORE; do printf '.skills/%s/ ' "$s"; done)
git rev-list --count "$BASE..$TAG"; git rev-list --count "$TAG..HEAD"
```

Per section (re-set the variables above in the same block):

1. Fork-stack conflicts and patched files: `cat .git/sync-upstream.conflicts` (conflict resolutions and step 9 re-applications); in a rehearsal also `cat .git/sync-upstream.rehearsal-notes`, under the heading `Rehearsal notes`; `git diff --stat "$BASE..$TAG" -- $REGISTER`; read `git diff "$BASE..$TAG" -- <file>` for each listed file; `git range-diff "$BASE..$MAIN_REF" "$TAG..HEAD"` shows how each stack commit changed. Name every register file that upstream changed, with its stat line. Rehearsal notes are not conflicts and do not go under judgment.
2. Core skill changes: `git diff --stat "$BASE..$TAG" -- $CORE_DIRS`, one line per changed core `SKILL.md` saying what changed; `git diff --no-renames --name-status "$BASE..$TAG" -- $CORE_DIRS | grep -E '^D'` (a deleted core skill goes under judgment). New references to non-core skills:
   ```bash
   NONCORE=$(git ls-tree --name-only "$TAG" .skills/ | sed 's#^\.skills/##' | grep -vxF "$(printf '%s\n' $CORE)")
   for s in $CORE; do git diff "$BASE..$TAG" -- ".skills/$s/" | grep '^+' | grep -owF "$NONCORE" | sort -u | sed "s/^/$s -> /"; done
   ```
3. CLI calls: `git diff "$BASE..$TAG" -- $CORE_DIRS .claude/hooks/ | grep '^+' | grep -oE 'obsidian-wiki [a-z][a-z-]*( [a-z][a-z-]*)?( --[a-z][a-z-]*)*' | sort -u`; `git diff --stat "$BASE..$TAG" -- obsidian_wiki/cli.py`; quote the C3 line from `.git/sync-check.txt`.
4. Memory surface and vault format: `git diff --stat "$BASE..$TAG" -- .skills/llm-wiki/references/MEMORY.md obsidian_wiki/memory.py obsidian_wiki/vault.py`; `git diff "$BASE..$TAG" -- obsidian_wiki/ .skills/llm-wiki/ | grep -E '^\+.*(frontmatter|\.manifest\.json|index\.md|hot\.md)'`.
5. Hooks: `git diff "$BASE..$TAG" -- .claude/hooks/ obsidian_wiki/hooks.py`.
6. Dependencies and network: `git diff "$BASE..$TAG" -- pyproject.toml`; `git diff "$BASE..$TAG" -- obsidian_wiki/ | grep -E '^\+.*(\b(urllib|httpx|socket|http\.client|aiohttp)\b|requests\.|import requests|from requests )'`.
7. New upstream skills: `git diff --no-renames --name-status "$BASE..$TAG" -- .skills/ | awk '$1=="A" && $2 ~ /^\.skills\/[^\/]+\/SKILL\.md$/ {print $2}'`; read each one's `description:` with `git show "$TAG:<path>"`. Recommend add or skip; adding is a later `fork:` commit, not part of this sync.

Write an empty section as `No changes.` Format (a real sync):

````markdown
# Sync <BASE> → <TAG>

Upstream commits: <n>. Fork stack commits: <m>.
Sync branch: sync/<TAG> at <full sha of HEAD>. Main before: <MAIN_BEFORE>.

## Fork-stack conflicts and patched files
## Core skill changes
## CLI calls
## Memory surface and vault format
## Hooks
## Dependencies and network
## New upstream skills

Needs your judgment:
- <item>   (or the single word: none)

```
<contents of .git/sync-check.txt>
```
````

A rehearsal report drops `Main before:` (it would name a local scratch branch) and adds a line under the header instead:

```markdown
Sync branch: sync/<TAG> at <full sha of HEAD>.
Rehearsal: --from <old> in <scratch path>; nothing pushed.
```

Without `--from`, write `Rehearsal: no --from in <scratch path>; nothing pushed.`

Needs your judgment lists every `FAIL` line left after step 9, a non-zero exit, a deleted or renamed core skill, a core skill newly pointing at a non-core skill, a new dependency or network call, a hook or memory-format change, and anything else that changes how a machine behaves after `FORK.md` › Update. A step 9 re-application that made its check pass is recorded in section 1, not here. Before presenting, check that every file from the section 1 `--stat` is named in the report.

## Finalize

Only after rule 2 is met for this report. Every block below sources `.git/sync-upstream.env` itself.

1. Saved values. In a new session without `.git/sync-upstream.env`, first write the approved report verbatim to `.git/sync-report-<TAG>.md`, then rebuild the env from it (set `TAG=` to the report's tag):
   ```bash
   TAG=
   R=".git/sync-report-${TAG:?}.md"
   SYNC_HEAD=$(sed -n 's/^Sync branch: sync\/[^ ]* at \([0-9a-f]\{40\}\)\..*/\1/p' "$R")
   MAIN_BEFORE=$(sed -n 's/.* Main before: \([0-9a-f]\{40\}\)\.$/\1/p' "$R")
   if [ -n "$SYNC_HEAD" ] && [ -n "$MAIN_BEFORE" ]; then
     printf '%s=%q\n' TAG "$TAG" SYNC_HEAD "$SYNC_HEAD" MAIN_BEFORE "$MAIN_BEFORE" > .git/sync-upstream.env && cat .git/sync-upstream.env
   else
     echo "STOP: the report lacks a Sync branch sha or a Main before: line"
   fi
   ```
   There is no fallback for `MAIN_BEFORE`: if the report has no `Main before:` line, STOP and prepare again.
2. Check the env against the report Ferdy approved:
   ```bash
   . .git/sync-upstream.env
   R=".git/sync-report-${TAG:?}.md"
   [ -s "$R" ] || echo "STOP: no report file $R"
   grep -q '^Rehearsal:' "$R" && echo "STOP: this is a rehearsal report"
   [ "$(sed -n 's/^Sync branch: sync\/[^ ]* at \([0-9a-f]\{40\}\)\..*/\1/p' "$R")" = "${SYNC_HEAD:?}" ] || echo "STOP: SYNC_HEAD is not the report's Sync branch sha"
   [ "$(sed -n 's/.* Main before: \([0-9a-f]\{40\}\)\.$/\1/p' "$R")" = "${MAIN_BEFORE:?}" ] || echo "STOP: no Main before: line, or it differs from MAIN_BEFORE"
   ```
3. Fetch and verify. Stop on any `STOP`; a moved `main` means prepare again:
   ```bash
   . .git/sync-upstream.env
   git fetch origin --tags
   [ "$(git rev-parse "origin/sync/${TAG:?}")" = "${SYNC_HEAD:?}" ] || echo "STOP: sync/$TAG is not the reviewed commit"
   [ "$(git rev-parse origin/main)" = "${MAIN_BEFORE:?}" ] || echo "STOP: main moved since prepare"
   git merge-base --is-ancestor "refs/tags/$TAG" "$SYNC_HEAD" || echo "STOP: $SYNC_HEAD is not built on $TAG"
   [ -z "$(git ls-remote --tags origin "refs/tags/fork-$TAG")" ] || echo "STOP: origin already has fork-$TAG"
   git rev-parse -q --verify "refs/tags/fork-$TAG" >/dev/null && echo "STOP: local tag fork-$TAG exists"
   O=$(git ls-remote --tags origin "refs/tags/$TAG" | cut -f1)
   [ -z "$O" ] || [ "$O" = "$(git rev-parse "refs/tags/$TAG")" ] || echo "STOP: origin's $TAG differs from upstream's"
   ```
4. Force-push with lease. If it is rejected, stop and prepare again; never retry without the lease.
   ```bash
   . .git/sync-upstream.env
   git push --force-with-lease="main:${MAIN_BEFORE:?}" origin "${SYNC_HEAD:?}:refs/heads/main"
   ```
5. Tag, push both tags, clean up, and reset local `main`. The chain stops at the first failure, so a failed tag push never deletes `sync/<TAG>`. `--cleanup=verbatim` keeps the report's `#` headings, which `git tag -F` strips by default. The upstream tag goes to origin too, so fresh fork clones have the base tag that `fork/check.py` and the CLI version need:
   ```bash
   . .git/sync-upstream.env
   git tag -a --cleanup=verbatim "fork-${TAG:?}" -F ".git/sync-report-${TAG:?}.md" "${SYNC_HEAD:?}" &&
   git push origin "refs/tags/${TAG:?}" "refs/tags/fork-${TAG:?}" &&
   git push origin --delete "refs/heads/sync/${TAG:?}" &&
   git switch -C main origin/main &&
   git branch -D "sync/${TAG:?}" &&
   rm -f .git/sync-upstream.env .git/sync-upstream.todo .git/sync-upstream.conflicts .git/sync-check.txt
   ```
   If the chain stopped, report where and leave the rest; `main` is already pushed, so do not rerun step 4.
6. Tell Ferdy that `main` is now at `fork-<TAG>` and to run `FORK.md` › Update on each machine.

## Rehearse

A sync in a scratch clone with nothing pushed. Inputs: the target tag (`TAG`); optional `--from <old tag>`, which first rebuilds the stack on an older tag so the rehearsal has a real gap to cover; optional `--ref <branch>` (default `main`), the branch of this fork clone that holds the stack; optional scratch path. The plan's rehearsal: `--from v2026.10.2`, target `v2026.10.3`.

1. Scratch clone of this fork clone (any clone works; `origin/<ref>` in the scratch is this clone's local `<ref>`), with push disabled on both remotes. Run it in the fork clone; set `REF`, and `SCRATCH_DIR` if Ferdy gave a path (it must be empty or not exist):
   ```bash
   REF=main
   SRC=$(git rev-parse --show-toplevel)
   SCRATCH_DIR=${SCRATCH_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/sync-rehearsal.XXXXXX")}
   SCRATCH=$SCRATCH_DIR/obsidian-wiki && echo "$SCRATCH"
   { git -C "$SRC" rev-parse --verify -q "refs/heads/$REF" >/dev/null || { echo "STOP: no local branch $REF in $SRC"; false; }; } &&
   git clone -q "$SRC" "$SCRATCH" && cd "$SCRATCH" &&
   git remote add upstream https://github.com/Ar9av/obsidian-wiki &&
   git remote set-url --push origin DISABLED && git remote set-url --push upstream DISABLED &&
   git fetch -q upstream --tags || echo "STOP: scratch clone setup failed"
   if git remote -v | grep '(push)' | grep -qv 'DISABLED (push)'; then echo "STOP: a push URL is not DISABLED"; fi
   ```
   Every later block starts with `SCRATCH=` set to the path it printed, then `cd`; no block in this mode pushes.
2. Stack branch. Check the subjects: autosquash `fixup!`/`squash!` commits; any other non-`fork:` subject stops the rehearsal:
   ```bash
   SCRATCH=/path/printed/by/step/1
   cd "${SCRATCH:?}" || exit 1
   REF=main
   BASE=$(git show "origin/$REF:FORK.md" | sed -n 's/^Upstream base: //p')
   [ -n "$BASE" ] || echo "STOP: no Upstream base line on origin/$REF"
   git switch -q -c rehearse/stack "origin/$REF"
   : > .git/sync-upstream.rehearsal-notes
   [ -z "$(git rev-list --merges "$BASE..HEAD")" ] || echo "STOP: merge commit in stack"
   git log --format=%s "$BASE..HEAD" | grep -vE '^(fork:|fixup! |squash! )' && echo "STOP: non-fork: commit in stack"
   if git log --format=%s "$BASE..HEAD" | grep -qE '^(fixup|squash)! '; then
     N=$(git rev-list --count "$BASE..HEAD")
     GIT_SEQUENCE_EDITOR=: GIT_EDITOR=true git rebase -q -i --autosquash "$BASE" &&
     echo "origin/$REF: autosquashed $N commits to $(git rev-list --count "$BASE..HEAD") on $BASE before the rehearsal" >> .git/sync-upstream.rehearsal-notes ||
     echo "STOP: autosquash failed"
   fi
   git log --format=%s "$BASE..HEAD" | grep -v '^fork:' && echo "STOP: subjects still not all fork:"
   git log --oneline "$BASE..HEAD"
   ```
3. With `--from`: build `rehearse/base` on the old tag by replaying `rehearse/stack` with the base line set to the old tag. Without `--from`, skip to step 4.
   ```bash
   SCRATCH=/path/printed/by/step/1
   cd "${SCRATCH:?}" || exit 1
   OLD=           # the --from tag, for example v2026.10.2
   : "${OLD:?set OLD to the --from tag}"
   BASE=$(git show rehearse/stack:FORK.md | sed -n 's/^Upstream base: //p')
   git merge-base --is-ancestor "$OLD" "$BASE" || echo "STOP: $OLD is not older than $BASE"
   git log --reverse --format=%H "$BASE..rehearse/stack" > .git/rehearse.todo
   git switch -q -c rehearse/base "$OLD"
   ```
   Then run this loop. A stack commit with nothing to patch on the old tag comes out empty; it is committed empty and logged as a rehearsal note. It stops on a conflict:
   ```bash
   SCRATCH=/path/printed/by/step/1
   cd "${SCRATCH:?}" || exit 1
   OLD=           # the --from tag, for example v2026.10.2
   : "${OLD:?set OLD to the --from tag}"
   for sha in $(cat .git/rehearse.todo); do
     subj=$(git log -1 --format=%s "$sha")
     if [ -n "$(git diff-tree --no-commit-id --name-only -r "$sha" -- FORK.md)" ]; then
       git cherry-pick -n "$sha" || { echo "CONFLICT (no-commit) at $sha"; break; }
       sed -i.bak "s/^Upstream base: .*/Upstream base: $OLD/" FORK.md && rm -f FORK.md.bak
       git add FORK.md && git commit -q -C "$sha" || { echo "COMMIT FAILED at $sha"; break; }
     elif ! git cherry-pick "$sha"; then
       if [ -z "$(git ls-files -u)" ] && git diff --cached --quiet; then
         git commit -q --allow-empty -C "$sha" || { echo "COMMIT FAILED at $sha"; break; }
         git cherry-pick --quit
         echo "$sha $subj: nothing to patch on $OLD; committed empty on rehearse/base" >> .git/sync-upstream.rehearsal-notes
       else
         echo "CONFLICT at $sha"; break
       fi
     fi
     sed -i.bak 1d .git/rehearse.todo && rm -f .git/rehearse.todo.bak
   done
   ```
   On a conflict, look at each conflicted hunk. If every one is only on text absent from the old tag (the stack commit edits lines that upstream added after `$OLD`), keep the HEAD side (the old tag's text) in those hunks and the pick's clean hunks elsewhere; for a file where every hunk is like that, `git checkout --ours -- <path>`. Then finish the pick. `git cherry-pick --continue` says `nothing to commit` and leaves the pick in progress when the resolution is empty, so this block detects that:
   ```bash
   SCRATCH=/path/printed/by/step/1
   cd "${SCRATCH:?}" || exit 1
   OLD=           # the --from tag, for example v2026.10.2
   : "${OLD:?set OLD to the --from tag}"
   SHA=$(head -n1 .git/rehearse.todo)
   PATHS='.skills/example/SKILL.md'   # the files you resolved
   git add -- $PATHS
   DONE=
   if GIT_EDITOR=true git cherry-pick --continue; then
     DONE=1; echo "$SHA $(git log -1 --format=%s "$SHA"): $PATHS: kept $OLD's text where the commit edits text absent from $OLD" >> .git/sync-upstream.rehearsal-notes
   elif [ -z "$(git ls-files -u)" ] && git diff --cached --quiet; then
     git commit -q --allow-empty -C "$SHA" && DONE=1 &&
     echo "$SHA $(git log -1 --format=%s "$SHA"): $PATHS: edits only text absent from $OLD; committed empty on rehearse/base" >> .git/sync-upstream.rehearsal-notes
     if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null || [ -d .git/sequencer ]; then git cherry-pick --quit; fi
   fi
   if [ -n "$DONE" ]; then sed -i.bak 1d .git/rehearse.todo && rm -f .git/rehearse.todo.bak; else echo "STOP: pick not finished"; fi
   ```
   Then rerun the loop. Any other conflict is itself a finding: run `git cherry-pick --abort`, report it and end. The rehearsal base is not a valid fork state on the old tag (for example `hooks/hooks.json` may name a script the old tag lacks), so do not run `fork/check.py` on it.
4. Run Prepare steps 2 to 10, with these lines at the top of the step 2 and step 3 blocks (`MAIN_REF=rehearse/stack` without `--from`, and switch to that branch first):
   ```bash
   MAIN_REF=rehearse/base
   TAG=           # the target tag, for example v2026.10.3
   REHEARSAL=1
   : "${TAG:?set TAG to the target tag}"
   ```
   Skip step 1 (the scratch clone is fresh) and step 11. In prepare step 5, a stack commit that is already empty on `rehearse/base` is committed empty and logged as a rehearsal note; a forward pick that comes out empty for any other reason still stops. Step 9 runs as in a real sync.
5. Write the report with the `Rehearsal:` line, show it to Ferdy, say it came from a rehearsal, and give the scratch path. Delete the scratch directory once he is done with it.

## Rollback

Only when Ferdy asks for it. `PREV` is the `fork-<tag>` tag to return to:

```bash
git fetch origin --tags
PREV=          # the fork-<tag> to return to, for example fork-v2026.10.3
CURRENT=$(git rev-parse origin/main)
if git rev-parse -q --verify "refs/tags/${PREV:?}^{commit}" >/dev/null; then
  git push --force-with-lease="main:${CURRENT:?}" origin "${PREV:?}^{}:refs/heads/main"
else
  echo "STOP: no tag $PREV"
fi
```

The spec writes this as `git push --force-with-lease origin fork/<previous tag>^{}:main`. This skill uses the renamed `fork-<tag>` tags, adds the explicit expected value (rule 3) and the full `refs/heads/main` destination, which git needs when the source is not a branch. Then tell Ferdy to run `FORK.md` › Update on each machine.
