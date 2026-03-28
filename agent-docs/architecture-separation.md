# Architecture Separation: Generic Agent Topology vs Security Domain

How the core business logic (multi-agent orchestration) is separated from the cybersecurity-specific context.

---

## High-Level Overview

```
+-----------------------------------------------------------------------------------+
|                                                                                   |
|                       ARISE-SEC-LION  ARCHITECTURE                                |
|              Generic Multi-Agent Topology  x  Security Domain                     |
|                                                                                   |
+---+--------------------------------------+----------------------------------------+
    |                                      |
    |  BOOTSTRAP (Composition Root)        |  The ONLY place that bridges both worlds
    |                                      |
    |  if settings.security.enabled:       |
    |      from plugins.security import    |  <-- sole cross-boundary imports
    |        SecurityDomainPlugin,         |
    |        SecBenchPromptStrategy        |
    |      plugin = SecurityDomainPlugin() |
    |      strategy = SecBenchPromptStrategy()
    |  else:                               |
    |      plugin = None                   |
    |      strategy = None                 |
    |                                      |
    |  ExecutionService(                   |
    |    domain_plugin=plugin,             |
    |    prompt_strategy=strategy, ...)    |  injected through separate protocols
    |                                      |
    +--------------------------------------+
                    |
    +---------------+---------------+
    |               |               |
    v               v               v
+-----------+ +-----------+ +----------------------------------+
|Presentation| |Query/CQRS | |Infrastructure (all generic)      |
|-----------| |-----------| |----------------------------------|
|CLI        | |Projections| |PostgresEventStore                |
|Renderers  | |FastAPI+SSE| |LiteLLMAdapter                   |
|Formatters | |React SPA  | |ClaudeAgentSDK/OpenHands/ADK     |
|           | |           | |PostgresSharedContext             |
+-----------+ +-----------+ +----------------------------------+
```

---

## Core Layer (Domain-Generic -- Zero security imports)

```
+===========================================================================+
|                                                                           |
|                    CORE  (Zero imports from plugins/)                      |
|                                                                           |
|  +---------------------------------------------------------------------+ |
|  |                      APPLICATION LAYER                               | |
|  |                                                                      | |
|  |  +-------------------+  +--------------------+  +------------------+ | |
|  |  | AgentOrchestrator |  | ExecutionService   |  | PromptBuilder    | | |
|  |  |-------------------|  |--------------------|  |------------------| | |
|  |  | assess_task()     |  | main loop          |  | TemplateChain    | | |
|  |  | evaluate_task()   |  | retry/escalation   |  | 4-tier templates | | |
|  |  | execute_task()    |  | concurrency        |  | delegates to     | | |
|  |  |                   |  | passes opaque      |  |   PromptStrategy | | |
|  |  |                   |  |   domain_context   |  | calls plugin.    | | |
|  |  |                   |  |                    |  |   enrich_prompt()| | |
|  |  +-------------------+  +--------------------+  +------------------+ | |
|  |                                                                      | |
|  |  +-------------------+  +--------------------+  +------------------+ | |
|  |  | ChildAgentFactory |  | AgentRepository    |  | ParentNotify     | | |
|  |  | AgentQueryService |  | EventBroadcaster   |  | Service          | | |
|  |  +-------------------+  +--------------------+  +------------------+ | |
|  +---------------------------------------------------------------------+ |
|                                                                           |
|  +---------------------------------------------------------------------+ |
|  |                        DOMAIN LAYER                                  | |
|  |                                                                      | |
|  |  +----------------------------+  +--------------------------------+  | |
|  |  | AgentSession               |  | Values (all generic)           |  | |
|  |  | (Event-Sourced Aggregate)  |  |                                |  | |
|  |  |                            |  | AgentRole: BOSS | PENDING |    |  | |
|  |  | ~30 DomainEvent types      |  |            MANAGER | WORKER    |  | |
|  |  | State machine:             |  | AgentStatus, Subtask           |  | |
|  |  |                            |  | NodeMessage (Briefing /        |  | |
|  |  | PENDING --> EXECUTE -> WORKER  |   Report / Handoff)           |  | |
|  |  |         \-> DECOMPOSE -> MGR   | AgentConfig, PromptTrace      |  | |
|  |  |                            |  |                                |  | |
|  |  | OCC via version            |  | HierarchyLimits                |  | |
|  |  +----------------------------+  |   max_depth, max_children      |  | |
|  |                                   |   max_total_agents             |  | |
|  |  +----------------------------+  |                                |  | |
|  |  | Domain Services            |  |   domain_context: object|None  |<----+
|  |  |                            |  |   (propagated via for_child()) |  | | |
|  |  | SubtaskParser              |  +--------------------------------+  | | |
|  |  | ConfigResolver             |                                      | | |
|  |  | TaskScheduler (Kahn's DAG) |                                      | | |
|  |  | ContextUpdateParser        |                                      | | |
|  |  +----------------------------+                                      | | |
|  +---------------------------------------------------------------------+ | |
|                                                                           | |
|  +---------------------------------------------------------------------+ | |
|  |                    PORTS  (Protocols / Interfaces)                    | | |
|  |                                                                      | | |
|  |  +---------------------------+  +----------------------------------+ | | |
|  |  | domain_plugin_port.py     |  | runtime_ports.py                 | | | |
|  |  |                           |  |                                  | | | |
|  |  | class DomainPlugin:       |  | LLMPort                         | | | |
|  |  |   infer_context() -> obj  |  | WorkerToolPort                  | | | |
|  |  |   enrich_prompt()         |  | CostCalculatorPort              | | | |
|  |  |   get_run_metadata()      |  | SharedContextPort               | | | |
|  |  |   get_tag_mappings()      |  | SiblingViewPort                 | | | |
|  |  |   get_provenance_patterns |  | ReconToolPort                   | | | |
|  |  |   prepare_*()/cleanup_*() |  | RealtimeCallbackPort            | | | |
|  |  |                           |  |                                  | | | |
|  |  | class PromptStrategy:     |  | event_store_port.py              | | | |
|  |  |   extend_boss_prompt()    |  | EventStoreConnect/Write/Read    | | | |
|  |  |   extend_manager_prompt() |  |                                  | | | |
|  |  |   extend_worker_prompt()  |  +----------------------------------+ | | |
|  |  |   extend_assessment_prompt|                                      | | |
|  |  +------------+--------------+                                      | | |
|  |               |                                                      | | |
|  +---------------------------------------------------------------------+ | |
|                  |                                                        | |
+==========================================================================+ | |
                   |                                                    OPAQUE|
                   |  implements                                         SLOT |
                   v                                                         |
                                                                             |
   PLUGINS / SECURITY DOMAIN                                                 |
                                                                             |
+===========================================================================+|
|  plugins/security/                                                        ||
|                                                                           ||
|  +---------------------------------------------------------------------+ ||
|  | SecurityDomainPlugin  (implements DomainPlugin)                      | ||
|  |                                                                      | ||
|  | infer_context(task) -> CVEInstanceInferenceService -> CVEInstance ----+-+
|  | enrich_prompt(prompt) -> appends Valgrind/KLEE tool instructions     |
|  | get_run_metadata()    -> {"instance_id": cve.instance_id}            |
|  +---------------------------------------------------------------------+
|
|  +--------------------+  +---------------------+  +--------------------+
|  | CVEInstance         |  | SecBenchPrompt      |  | SecurityTool       |
|  |                     |  | Strategy            |  | Registry           |
|  | instance_id         |  |                     |  |                    |
|  | repo, lang          |  | Detects SEC-bench   |  | Valgrind, KLEE     |
|  | sanitizer           |  | phase from briefing |  | Phase applicability|
|  | bug_description     |  | ancestry:           |  | Command examples   |
|  | base_commit         |  |   builder            |  +--------------------+
|  | build_sh, patch     |  |   exploiter          |
|  | docker_image_name   |  |   fixer              |  +--------------------+
|  | expected_errors     |  |                     |  | BenchmarkResult    |
|  +--------------------+  | Returns None when   |  | 3-stage tracking   |
|                           | not SEC-bench ->    |  | (build/exploit/fix)|
|  +--------------------+  | fallback to generic |  +--------------------+
|  | CVEInstanceInference|  +---------------------+
|  | Service             |
|  | Regex extraction of |
|  | CVE, repo, commit,  |
|  | sanitizer, language |
|  +--------------------+
|
|  +---------------------------------------------------------------------+
|  | prompts/domains/secbench/             (Tier 4 -- Domain Templates)   |
|  |                                                                      |
|  | boss.j2 --- 3-phase decomposition (build -> exploit -> fix)          |
|  | cve.j2  --- CVE instance context (instance data, env, criteria)      |
|  | tools.j2 -- Security tool guidance (Valgrind/KLEE usage)            |
|  |                                                                      |
|  | manager/                          worker/                            |
|  |   builder.j2   -- build phase      builder.j2   -- compile/reproduce|
|  |   exploiter.j2 -- exploit phase    exploiter.j2 -- trigger sanitizer|
|  |   fixer.j2     -- fix phase        fixer.j2     -- patch vuln       |
|  +---------------------------------------------------------------------+
|
+===========================================================================+
```

---

## Prompt Assembly -- 4-Tier Layering

```
+-----------------------------------------------------------+
|                    FINAL PROMPT                            |
|                                                           |
|  +-----------------------------------------------------+ |
|  |  Tier 1: System Identity    (prompts/system.j2)      | |  <-- Generic
|  +-----------------------------------------------------+ |
|  |  Tier 2: Role Persona       (prompts/roles/*.j2)     | |  <-- Generic
|  +-----------------------------------------------------+ |
|  |  Tier 3: Operation Instructions                       | |  <-- Generic
|  |           (prompts/operations/*.j2)                   | |
|  |           + Context (sibling.j2, workspace.j2)        | |
|  +-----------------------------------------------------+ |
|  |  Tier 4: PromptStrategy Extension (if wired)          | |  <-- SECURITY
|  |           SecBenchPromptStrategy detects phase         | |
|  |           extends the base chain OR returns None       | |
|  |           (None -> keeps Tiers 1-3 unchanged)          | |
|  +-----------------------------------------------------+ |
|  |  Post-build Enrichment                                | |  <-- SECURITY
|  |           plugin.enrich_prompt() appends tool          | |
|  |           instructions (Valgrind/KLEE)                | |
|  +-----------------------------------------------------+ |
+-----------------------------------------------------------+
```

---

## Data Flow -- How Security Context Propagates

```
    Task: "Reproduce CVE-2023-XXXX in project/repo ..."
                          |
                          v
    +-------------------------------------+
    | SecurityDomainPlugin.infer_context  |
    | Regex parse task text               |
    | OR load --cve-file JSON             |
    +------------------+------------------+
                       |
                       v
                CVEInstance (frozen Pydantic)
                ----------------------------
                instance_id, repo, lang,
                sanitizer, bug_description...
                       |
                       |  stored as opaque `object`
                       v
    +-------------------------------------+
    | HierarchyLimits.domain_context      |  <-- core sees this as `object | None`
    | (set at run creation)               |
    +------------------+------------------+
                       |
                       |  for_child() propagates
                       v
    +-------------------------------------+
    | BOSS                                |
    |   domain_context = CVEInstance       |--> SecBenchPromptStrategy.extend_boss_prompt()
    |                                     |    appends boss.j2 + cve.j2 onto the base chain
    |   decomposes into 3 phases          |
    +------+----------+----------+--------+
           |          |          |
           v          v          v
    +----------+ +----------+ +----------+
    | MANAGER  | | MANAGER  | | MANAGER  |
    | (builder)| |(exploiter| | (fixer)  |
    |          | |)         | |          |   Each gets domain_context
    | spawns   | | spawns   | | spawns   |   via for_child()
    | WORKERs  | | WORKERs  | | WORKERs  |
    +----+-----+ +----+-----+ +----+-----+
         |            |            |
         v            v            v
    Phase-specific prompts from
    prompts/domains/secbench/worker/{builder,exploiter,fixer}.j2
    + enrich_prompt() appends tool instructions
```

---

## Boundary Summary

```
+------------------------------+-----------------------------------+---------------------------+
| Boundary                     | Mechanism                         | Key File(s)               |
+------------------------------+-----------------------------------+---------------------------+
| Core never imports plugins   | Convention + grep-verified        | All of core/              |
| Domain context is opaque     | object | None typing              | core/domain/values/       |
|                              |                                   |   limits.py               |
| Plugin protocol lives in core| DomainPlugin(Protocol), 8 methods| core/ports/               |
|                              |                                   |   domain_plugin_port.py   |
| Strategy protocol in core    | PromptStrategy(Protocol),        | core/application/services/|
|                              | 4 methods                        |   prompt_strategy.py      |
| Bootstrap is the only bridge | Single import from plugins.sec   | bootstrap/bootstrap.py    |
| Templates separated by dir   | prompts/roles/ (generic) vs      | prompts/ directory tree   |
|                              | prompts/domains/secbench/ (sec)  |                           |
| Config is optional           | security.enabled gate             | config/settings.py +      |
|                              |                                   | bootstrap/bootstrap.py    |
+------------------------------+-----------------------------------+---------------------------+
```

---

## Key Design Properties

1. **Core is security-ignorant** -- Zero imports from `plugins/` anywhere in `core/`. The orchestration engine works for any multi-agent task.

2. **Plugin is optional** -- Setting `security.enabled=false` means no plugin or prompt strategy is wired. The system runs in pure generic mode because `PromptBuilder` skips Tier 4 extension when `strategy=None`.

3. **Opaque context slot** -- `HierarchyLimits.domain_context: object | None` carries domain data through the entire agent hierarchy without the core ever inspecting or downcasting it. Only the plugin's strategy knows the concrete type.

4. **Strategy pattern for prompts** -- `PromptStrategy` extends `TemplateChain | None`. The security strategy appends SEC-bench templates for matching phases; `None` leaves the generic Tier 1-3 prompt untouched.

5. **Single bridge point** -- Only `bootstrap/bootstrap.py` crosses the boundary with `from plugins.security import ...`. This is the composition root doing its job.

6. **Fully removable** -- Delete `plugins/security/` + `prompts/domains/secbench/` + the config section, and the system continues to function as a generic multi-agent orchestration platform.

The architecture is a textbook **hexagonal / ports-and-adapters** design where the security domain is a **pluggable bounded context** connected through two separate protocol seams (`DomainPlugin` + `PromptStrategy`) and one opaque data slot (`domain_context`).
