# SEC-bench Patch Image Runtime Convention

This document records the verified SEC-bench convention used by Arise for
`cheshire0814/secb-tools:*-patch` images.

## Baseline

| Fact | Explanation | Evidence |
|---|---|---|
| Arise uses SEC-bench patch-task images as the upstream base. | `CVEInstance.docker_image` constructs `hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch` unless an override is provided. | `plugins/security/cve_instance.py:49-52` |
| Arise preserves the upstream image tag when security tools are enabled. | `resolve_secbench_image()` splits the Docker tag from the base image and appends it to the local `secb-tools` tag, so upstream `:patch` becomes `secb-tools:<id>-patch`. | `plugins/security/image_resolver.py:26-42` |
| The local build path creates `secb-tools:<id>-patch`. | The build script expands shorthand instance IDs to `hwiwonlee/secb.eval.x86_64.<id>:patch`. | `deployment/build-secbench-tools.sh:48-51` |
| The pushed `cheshire0814` images are `*-patch` images. | The push script tags `secb-tools:<id>-patch` as `<registry>/secb-tools:<id>-patch`. | `deployment/push-study-images.sh:56-64` |
| Local Docker inventory matched the patch-image naming convention. | Docker inspection showed local tags such as `cheshire0814/secb-tools:libxml2.ossfuzz-42496802-patch`, `cheshire0814/secb-tools:gpac.cve-2023-46001-patch`, and `cheshire0814/secb-tools:upx.ossfuzz-42531672-patch`. | `docker images --format '{{.Repository}}:{{.Tag}}'` in session |
| Local Docker contents matched patch-task semantics. | `cheshire0814/secb-tools:libxml2.ossfuzz-42496802-patch` had `/testcase/poc`, did not have `/testcase/model_patch.diff`, and `secb repro` invoked `/testcase/poc`. | `docker run --rm --read-only --network none --entrypoint /bin/sh cheshire0814/secb-tools:libxml2.ossfuzz-42496802-patch ...` in session |

## Core Commands

| Command | Meaning | Flow | Evidence |
|---|---|---|---|
| `secb build` | Build the vulnerable project in the SEC-bench environment. | Applies `/testcase/repo_changes.diff` if present, then calls `/usr/local/bin/compile`. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:4-35` |
| `/usr/local/bin/compile` | OSS-Fuzz build wrapper used by SEC-bench. | Sets sanitizer/fuzzer-related environment and executes `$SRC/build.sh`. | `../SEC-bench/secb/preprocessor/build_base_images.py:87-94`; Docker inspection showed Google copyright header and `BUILD_CMD="bash -eux $SRC/build.sh"`. |
| `secb repro` | Run the vulnerability reproducer. | Executes the instance-specific trigger command against PoC files in `/testcase`. In Arise patch images, this is already filled. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:38-45`; Docker inspection showed `libxml2` runs `/src/libxml2/xmllint --sax --sax1 /testcase/poc`. |
| `secb patch` | Apply the candidate model patch. | Applies `/testcase/repo_changes.diff` if present, then applies `/testcase/model_patch.diff`. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:47-68` |

## Directory Contract

| Path / Variable | Role | Explanation | Evidence |
|---|---|---|---|
| `/src` / `$SRC` | Source tree root. | OSS-Fuzz and SEC-bench expect project repositories and `/src/build.sh` under `/src`. The instance `work_dir` is usually `/src/<project>`. | `../SEC-bench/secb/preprocessor/project.py:1527-1536`; live Docker inspection showed `SRC=/src` and `/src` exists. |
| `/src/<project>` | Project working directory. | `secb build`, `secb repro`, and `secb patch` usually run from this directory. SEC-bench normalizes missing or relative work directories under `/src`. | `../SEC-bench/secb/preprocessor/project.py:1527-1536`; `../SEC-bench/secb/preprocessor/templates/Dockerfile.instance.j2:20-21` |
| `/src/build.sh` | Build script location. | `/usr/local/bin/compile` executes `$SRC/build.sh`; project-specific scripts may `cd /src/<project>` internally. | Docker inspection showed `BUILD_CMD="bash -eux $SRC/build.sh"` in `/usr/local/bin/compile`; sampled images had populated `/src/build.sh`. |
| `/testcase` | SEC-bench artifact directory. | Stores PoC inputs, `base_commit_hash`, `repo_changes.diff`, `model_patch.diff` when supplied, package lists, and other testcase artifacts. | `../SEC-bench/secb/preprocessor/templates/Dockerfile.instance.j2:8-9`; live Docker inspection showed `/testcase` exists. |
| `/work` / `$WORK` | OSS-Fuzz scratch/build artifact directory. | Available for build/runtime scratch outputs and persisted by Arise bind mounts. It is not the same as the project `work_dir`; `work_dir` usually points under `/src`. | Live Docker inspection showed `WORK=/work` and `/work` exists; Arise maps `/work` in `plugins/security/container_runtime.py:32-45`. |
| `/out` / `$OUT` | OSS-Fuzz output directory. | Used by OSS-Fuzz tooling for built fuzz targets, runtime helpers, and symbolizer placement. Some SEC-bench helpers mention `/out/fuzzer` as the expected style for fuzz-target repro commands. | Live Docker inspection showed `OUT=/out` and `/out` exists; `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:44` mentions `/out/fuzzer`. |
| `/arise-run` | Arise-only workspace mount. | Arise mounts run-scoped helper/config files here for worker execution. This is not a SEC-bench paper convention. | `plugins/security/container_runtime.py:32-33`; `plugins/security/mcp/security_tools_server.py:66-69` |

## Builder, Exploiter, Fixer Flow

| Phase | SEC-bench Convention | Command Flow | Main Artifacts | Evidence |
|---|---|---|---|---|
| Builder | Ensure the vulnerable base commit builds with sanitizer instrumentation. Edit `/src/build.sh` only when needed. | Run `cd <work_dir> && secb build`. `secb build` replays `repo_changes.diff` if present and delegates the actual build to `/usr/local/bin/compile`, which runs `$SRC/build.sh`. | `/src/build.sh`, `/testcase/base_commit_hash`, `/testcase/repo_changes.diff`, `/testcase/packages.txt` | SEC-bench helper: `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:4-35`; SecVerifier prompt: `../SecVerifier/prompts/instructions/builder_agent_instruction.j2:22-57` |
| Exploiter | Use existing PoC artifacts first and ensure `secb repro` triggers the expected sanitizer error. | Run `secb build` if binaries are missing, then run `secb repro`. In Arise patch images, PoC files and a filled `secb repro` command are already baked in. | `/testcase/poc*`, `/usr/local/bin/secb` `repro()` | SecVerifier prompt: `../SecVerifier/prompts/instructions/exploiter_agent_instruction.j2:13-25`; Docker inspection showed PoC files and filled `repro()` in sampled images. |
| Fixer | Produce the candidate security fix as `/testcase/model_patch.diff` and verify that the PoC no longer triggers sanitizer output. | Reset to base, apply `/testcase/repo_changes.diff` if present, apply `/testcase/model_patch.diff`, run `secb build`, then run `secb repro`. | `/testcase/model_patch.diff` | SEC-bench helper: `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:47-68`; SecVerifier prompt: `../SecVerifier/prompts/instructions/fixer_agent_instruction.j2:32-45` |

## Artifact Roles

| Artifact | Role | Explanation | Evidence |
|---|---|---|---|
| `/src/build.sh` | Project build recipe. | Contains project-specific build steps. It is executed by `/usr/local/bin/compile`, not meant to replace `compile`. | `../SEC-bench/secb/preprocessor/project.py:1474-1497`; Docker inspection of sampled images. |
| `/usr/local/bin/compile` | Build environment wrapper. | Inherited from OSS-Fuzz base-builder; configures sanitizer/fuzzer environment and runs `$SRC/build.sh`. | `../SEC-bench/secb/preprocessor/build_base_images.py:87-94`; Docker inspection. |
| `/usr/local/bin/secb` | SEC-bench command harness. | Dispatches `build`, `repro`, and `patch`. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:70-92` |
| `/testcase/repo_changes.diff` | Build/setup baseline delta. | Replayed before build and patch so the vulnerable baseline can be reconstructed after a reset. It is not the vulnerability fix. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:15-24`; `../SecVerifier/prompts/instructions/builder_agent_instruction.j2:48-52` |
| `/testcase/model_patch.diff` | Candidate security fix. | The Fixer deliverable. It is absent from Arise patch images and must be produced by the model/agent. | `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:62`; `../SecVerifier/prompts/instructions/fixer_agent_instruction.j2:32-45` |
| `/testcase/poc*` | Golden trigger input. | Present in Arise patch images and used by `secb repro`. Agents should use these before creating new PoC inputs. | Docker inspection of `libxml2`, `gpac`, `upx`, `mruby`, and `openexr` patch images. |

## Current Arise Prompt Facts

| Fact | Explanation | Evidence |
|---|---|---|
| The current Builder prompt runs `/src/build.sh` directly. | This differs from the SEC-bench convention where validation is through `secb build`, which delegates to `compile`. | `prompts/domains/secbench/phases/build.j2:32-37`; `prompts/domains/secbench/worker/builder.j2:29-44` |
| The current Builder prompt treats `secb build()` as optional reference material. | The prompt asks agents to port useful logic into `build.sh` instead of treating `secb build` as the primary command. | `prompts/domains/secbench/phases/build.j2:12-14`; `prompts/domains/secbench/worker/builder.j2:31-33` |
| The current Exploiter prompt uses `repro.sh` as the deliverable. | This creates a separate reproducer contract from SEC-bench's baked `secb repro` command. | `prompts/domains/secbench/phases/exploit.j2:4-11` |
| The current Exploiter prompt still prioritizes provided PoC artifacts. | It says to use PoC artifacts under `/testcase` first and only create new PoCs when none exist. | `prompts/domains/secbench/phases/exploit.j2:6-20` |
| The current Fixer prompt applies Builder changes before model patch. | This matches SEC-bench replay order. | `prompts/domains/secbench/phases/fix.j2:13-20`; `prompts/domains/secbench/phases/fix.j2:38-40` |
| The current Fixer prompt generates `model_patch.diff` from source edits. | This matches the SEC-bench candidate patch artifact role. | `prompts/domains/secbench/phases/fix.j2:4-10`; `prompts/domains/secbench/worker/fixer.j2:96-103` |
