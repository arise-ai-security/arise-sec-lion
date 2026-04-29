---
description: Create a SEC-bench CVE fixture from a GitHub issue URL or CVE number — builds Docker image, pushes to DockerHub, generates fixture JSON
argument-hint: <github-issue-url-or-cve-number>
allowed-tools: [Read, Write, Edit, Glob, Grep, Bash, Agent, WebFetch, WebSearch, TaskCreate, TaskUpdate]
---

# SEC-bench Fixture Creator

An **optional shortcut** for the fresh-authoring fixture workflow: research the vulnerability, build an ASan-instrumented Docker image, push it to DockerHub, and write the fixture JSON. The same steps can be done by hand — this skill just chains them together.

> **Non-negotiable invariant: the Docker image must contain NO fix.**
> The image, the source tree it ships, the `.git` metadata, the DockerHub description, and the fixture JSON's free-text fields must give an agent **zero hints** about how to patch the bug. The agent under test is supposed to derive the fix from scratch by reading the code and the sanitizer report — anything that lets it shortcut that work counts as cheating.
>
> This invariant applies **whether or not an upstream fix exists yet**:
> - Fix is public → strip `.git`, verify the fix commit/PR/branch is unreachable, and keep fix references out of all bundled text.
> - No fix yet → still strip `.git` (a fix may land between image build and agent run; the cloned tree may also carry refs to draft fix branches or PRs), and do not write hint-laden TODO comments or fix sketches into the harness, build script, bug description, or DockerHub copy.
>
> If you cannot guarantee this, **do not push the image**. Re-build with the leak closed first.

> **New to this repo?** Read `README.md` first — §6 shows the two paths for adding a CVE (dataset fetch vs. fresh authoring) and explicitly frames this skill as the optional shortcut on the fresh-authoring path. §7 explains the DockerHub image conventions the skill uses.
>
> **Output location:** This skill writes `plugins/security/tests/fixtures/<instance_id>.json`. That is the same path §4 of the README reads from — once the skill finishes, the fixture is immediately runnable by hand (`bash deployment/build-secbench-tools.sh <json>` + `docker exec arise-app python main.py run …`) or via the `/secbench-run` shortcut.

## Arguments

The user provided: $ARGUMENTS

If no GitHub issue URL or CVE number is given, ask for one before proceeding.

## Phase 1: Research the vulnerability

1. **Fetch the GitHub issue** (use `WebFetch` or `Agent` with web tools):
   - Full bug description, PoC details, ASan/sanitizer output if available
   - Identify: project repo, vulnerable file/function, root cause
   - Find the vulnerable commit hash (pre-patch)
   - Find the fix commit/PR **if one exists** (to verify we do NOT leak the patch into the image)
   - **No upstream fix yet?** That is fine — many recently-filed issues have no patch. Record this explicitly (`fix_commit: none`) and skip the "verify the patch works" sub-step in Phase 3. The fixture is still valid: it only needs to reproduce the crash. The agent under test will be the first to produce a fix, which the harness will then judge by "does the rebuilt binary stop crashing without breaking compilation."
   - **Patch-leakage hardening still applies even with no known fix.** Do not assume "no patch exists upstream" means there is nothing to leak — a patch may land between when you build the image and when an agent runs against it, and the cloned tree may already contain refs to fix branches/PRs. Strip `.git` regardless (Phase 2d).

2. **Fetch the vulnerable source code** at the identified commit:
   - The vulnerable file (e.g., the `.c` file with the bug)
   - Header files needed for the public API
   - Identify dependencies needed to compile a minimal harness

3. **Determine key parameters**:
   - `project_name`: lowercase project identifier (e.g., `libretro-common`)
   - `instance_id`: `{project_name}.{cve-id}` (e.g., `libretro-common.cve-2025-9809`)
   - `repo`: GitHub `owner/repo` (e.g., `libretro/libretro-common`)
   - `base_commit`: the vulnerable commit hash
   - `sanitizer`: usually `address` for memory bugs
   - `work_dir`: `/src/{project_name}` (where the source lives in the container)
   - Buffer/variable sizes that affect PoC crafting (e.g., `PATH_MAX_LENGTH`)

## Phase 2: Create build artifacts

Create a temporary build directory at `/tmp/{instance_id}-build/`.

### 2a. Harness program (`harness.c`)

Write a minimal C program that:
- Includes the project's public header
- Calls the function that triggers the vulnerable code path
- Takes a PoC file path as `argv[1]`
- Example pattern:
```c
#include <stdio.h>
#include <project/header.h>

int main(int argc, char *argv[]) {
    if (argc < 2) { fprintf(stderr, "Usage: %s <poc_file>\n", argv[0]); return 1; }
    /* Call the public API that dispatches to the vulnerable function */
    result_t *r = public_api_function(argv[1], ...);
    if (r) cleanup_function(r);
    return 0;
}
```

### 2b. PoC file (`poc.*`)

Craft a minimal input that triggers the vulnerability:
- For buffer overflows: input that exceeds the target buffer size
- For NULL derefs: input that causes NULL pointer access
- **IMPORTANT**: Check the actual buffer size in the build environment (may differ from docs)

### 2c. Build script (`build.sh`)

```bash
#!/bin/bash -eu
set -eu
cd /src/{project_name}
# $CC and $CFLAGS are set by the base image's compile script with ASan flags
$CC $CFLAGS \
    -I include \
    /src/harness.c \
    {list of .c source files needed} \
    -o /src/harness_bin \
    -lm
```

**Dependency resolution**: Start with the minimum set of `.c` files. If linking fails with
undefined references, add the missing source files and rebuild.

### 2d. Dockerfile

```dockerfile
FROM hwiwonlee/secb.base:latest
ENV FUZZING_LANGUAGE=c
RUN apt-get update && apt-get install -y git {additional-deps}
RUN git clone https://github.com/{owner}/{repo}.git {project_name} \
    && cd {project_name} \
    && git checkout {vulnerable_commit} \
    && rm -rf .git \
    && git init \
    && git config user.email "secbench@local" \
    && git config user.name "secbench" \
    && git add -A && git commit -m "{project_name} at {short_commit} (pre-patch)"
WORKDIR $SRC/{project_name}
COPY harness.c $SRC/harness.c
COPY build.sh $SRC/
RUN mkdir -p /testcase
COPY poc.{ext} /testcase/poc.{ext}
```

Key rules:
- **Always set `ENV FUZZING_LANGUAGE=c`** (or appropriate language) — the base image's `compile` script requires it
- **CRITICAL: Strip git history to prevent patch leakage** — after checking out the vulnerable commit,
  `rm -rf .git && git init && git add -A && git commit` to create a clean single-commit repo. Without this,
  `git log --all` exposes the upstream fix commit (or fix-branch hints, or refs to a draft PR), allowing agents to cheat.
  **This step is mandatory regardless of whether a public fix exists today** — a patch may land upstream between image build time and agent run time, and even pre-fix repos may contain branches/tags/PR refs that hint at the eventual fix.
- **Never include the upstream fix** — the image must reproduce the crash
- Checkout the **pre-patch** commit only
- After building, **verify** no fix is reachable:
  - If a fix commit is known: `git log --oneline --all | grep {fix_commit}` should return nothing
  - If no fix exists yet: still confirm the repo collapses to a single commit (`git log --oneline --all | wc -l` should print `1`) and that no remote refs survived (`git remote -v` should be empty, `ls .git/refs/remotes` should not exist)

## Phase 3: Build and test the Docker image

### 3a. Build the image
```bash
cd /tmp/{instance_id}-build
docker build -t songtli/secb.eval.x86_64.{project_name}.{cve_id}:patch .
```

### 3b. Test build inside container
```bash
docker run --rm <image> bash -lc '
chmod +x /src/build.sh
/usr/local/bin/compile 2>&1 | tail -10
echo "=== compile exit: $? ==="
'
```
If the build fails with undefined references, add missing `.c` files to `build.sh` and rebuild.

### 3c. Test crash reproduction
```bash
docker run --rm <image> bash -lc '
chmod +x /src/build.sh
/usr/local/bin/compile 2>&1 | tail -1
/src/harness_bin /testcase/poc.{ext} 2>&1
'
```

**Expected**: ASan should report the vulnerability (e.g., `stack-buffer-overflow`, `heap-buffer-overflow`, `SEGV`).

**If no crash**: The PoC may be undersized. Check the actual buffer/limit sizes inside the container:
```bash
docker run --rm <image> bash -c 'grep -n "DEFINE_VALUE" /src/{project}/include/header.h'
```

### 3d. Verify no patch leakage (anti-cheat audit)

Run this for **every** fixture, even when no upstream fix exists yet — the goal is to prove the cloned tree (and everything else shipped in the image) carries no metadata that hints at how to fix the bug. An agent must not be able to recover a fix from `git log`, branch names, remote refs, ASan-suppression files, comments planted in the harness, or text bundled in the fixture JSON.

```bash
docker run --rm <image> bash -c '
cd /src/{project_name}
echo "=== Git log (should be exactly one commit) ==="
git log --oneline --all
COMMITS=$(git log --oneline --all | wc -l)
[ "$COMMITS" = "1" ] && echo "Single commit (good)" || echo "MULTIPLE COMMITS — LEAK RISK"

echo "=== Remote refs (should be empty) ==="
git remote -v
ls .git/refs/remotes 2>/dev/null && echo "REMOTE REFS PRESENT — LEAK RISK" || echo "No remote refs (good)"

echo "=== Branches/tags (should only be one local branch, no fix-* / patch-* / pr-*) ==="
git branch -a
git tag

# Only run this block when a fix commit is known — skip for issues with no upstream patch.
# echo "=== Search for fix commit ==="
# git log --oneline --all | grep -i "{fix_commit_short}" && echo "LEAK DETECTED!" || echo "Clean (good)"
'
```

**Also grep the bundled artifacts for fix-shaped hints:**

```bash
docker run --rm <image> bash -c '
  echo "=== Hint-word scan in /src and /testcase ==="
  # Common cheat-words that should NOT appear in the harness, build script, PoC, or any docs we ship.
  # Whitelist legitimate uses (e.g. the literal word "patch" in the secb_sh template) by inspecting hits.
  grep -RInE "TODO\\(fix\\)|FIXME|cheat|workaround|fix[-_ ]commit|pull/[0-9]+|PR ?#?[0-9]+|see commit [0-9a-f]{7,}" \
       /src/harness* /src/build.sh /testcase 2>/dev/null \
    && echo "POTENTIAL HINT LEAK — review hits above" \
    || echo "No obvious hint words (good)"
'
```

If any check fails, fix the Dockerfile / build artifacts (see Phase 2d) and rebuild — do **not** ship the image. **The fixture JSON's `bug_description`, `bug_report`, and `sanitizer_report` fields must also be reviewed by hand** for sentences like "the fix is to add a NULL check at line N" — paraphrase or remove them. State the symptom, not the remedy.

### 3e. Capture the ASan report

Save the full ASan output — it goes into the fixture's `sanitizer_report` field.

## Phase 4: Push to DockerHub

```bash
docker push songtli/secb.eval.x86_64.{project_name}.{cve_id}:patch
```

### Set DockerHub description

Use the DockerHub API to set the repository description:
```bash
AUTH=$(cat ~/.docker/config.json | python3 -c "
import sys, json, base64
config = json.load(sys.stdin)
auth = config.get('auths', {}).get('https://index.docker.io/v1/', {}).get('auth', '')
if auth: print(base64.b64decode(auth).decode())
")
USERNAME=$(echo "$AUTH" | cut -d: -f1)
PASSWORD=$(echo "$AUTH" | cut -d: -f2-)
TOKEN=$(curl -s -X POST "https://hub.docker.com/v2/users/login/" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")

curl -s -X PATCH "https://hub.docker.com/v2/repositories/songtli/secb.eval.x86_64.{project_name}.{cve_id}/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "{CVE_ID}: {short_description} ({project_name})",
    "full_description": "## {CVE_ID}\n\n{detailed_description}\n\n**GitHub Issue:** {issue_url}\n**Base image:** hwiwonlee/secb.base:latest\n**Vulnerable commit:** {commit}\n**Fix:** {fix_reference_or_none_yet}"
  }'
```

## Phase 5: Generate the fixture JSON

Write to `plugins/security/tests/fixtures/{project_name}.{cve_id}.json`:

```json
{
  "instance_id": "{project_name}.{cve_id}",
  "repo": "{owner}/{repo}",
  "project_name": "{project_name}",
  "docker_image_override": "songtli/secb.eval.x86_64.{project_name}.{cve_id}:patch",
  "lang": "c++",
  "work_dir": "/src/{project_name}",
  "sanitizer": "address",
  "bug_description": "{description from issue}",
  "base_commit": "{vulnerable_commit_hash}",
  "build_sh": "{contents of build.sh, escaped for JSON}",
  "secb_sh": "{standard secb_sh template with instance_id and harness path}",
  "dockerfile": "{contents of Dockerfile, escaped for JSON}",
  "exit_code": {exit_code_from_repro},
  "sanitizer_report": "{captured ASan output, escaped for JSON}",
  "bug_report": "{formatted bug report from issue}"
}
```

### secb_sh template

Always use this template (replace `{instance_id}` and the repro command):

```bash
#!/bin/bash

build() {
    echo "BUILDING THE PROJECT..."
    if /usr/local/bin/compile 1>/dev/null; then
        echo "BUILD COMPLETED SUCCESSFULLY!"
    else
        echo "BUILD FAILED!"
        exit 1
    fi
}

repro() {
    echo "REPRODUCING THE ISSUE FOR {instance_id}..."
    /src/harness_bin /testcase/poc.{ext}
    # NOTE: YOU SHOULD NOT RETURN/EXIT 0 IN THIS FUNCTION.
}

patch() {
    echo "PATCHING THE PROJECT..."
    cd /src/{project_name}
    if git apply /testcase/model_patch.diff; then
        echo "PATCH APPLIED SUCCESSFULLY!"
    else
        echo "PATCH APPLICATION FAILED!"
        exit 1
    fi
}

if [ "$#" -ge 1 ]; then
    command="$1"
    case "$command" in
        build) build "$@" ;;
        repro) repro "$@" ;;
        patch) patch "$@" ;;
        *) echo "Unknown command: $command"; echo "Usage: secb [build|repro|patch]"; exit 1 ;;
    esac
else
    echo "Usage: secb [build|repro|patch]"
    exit 1
fi
```

## Phase 6: Verification

Verify the fixture loads correctly:
```bash
cd /home/songli/arise-sec-lion && python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('cve_instance', 'plugins/security/cve_instance.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cve = mod.CVEInstance.from_json_file('plugins/security/tests/fixtures/{instance_id}.json')
print(f'instance_id: {cve.instance_id}')
print(f'docker_image: {cve.docker_image}')
print(f'expected_sanitizer_error: {cve.expected_sanitizer_error}')
print(f'has_dockerfile: {cve.has_dockerfile}')
print(f'has_build_script: {cve.has_build_script}')
"
```

## Summary checklist

At the end, report:
- [ ] Vulnerability researched (issue URL, CVE, root cause)
- [ ] Docker image built and tested (crash reproduced)
- [ ] Image pushed to DockerHub with description
- [ ] Fixture JSON written to `plugins/security/tests/fixtures/`
- [ ] CVEInstance model loads the fixture correctly
- [ ] Image name, PoC, and harness details

## Reference: existing fixtures

For reference patterns, see:
- `plugins/security/tests/fixtures/libredwg.cve-2020-21816.json` (autotools project)
- `plugins/security/tests/fixtures/jq.cve-2023-50246.json` (autoconf project)
- `plugins/security/tests/fixtures/exiv2.cve-2018-19607.json` (cmake project)
- `plugins/security/tests/fixtures/libretro-common.cve-2025-9809.json` (header-only C library, custom harness)

## Notes

- The `docker_image_override` field in the fixture JSON overrides the default `hwiwonlee/...` image name
- The base image `hwiwonlee/secb.base:latest` provides clang, ASan, and the `/usr/local/bin/compile` wrapper
- `PATH_MAX_LENGTH` and similar constants may differ between platforms — always check inside the container
- For library-only projects (no standalone binary), you must write a harness that exercises the vulnerable API
