# Design Principles & Philosophies

This project strictly adheres to industry-standard design principles. **Always apply these when writing code.**

---

## SOLID Principles (Robert C. Martin)

> **Source:** Robert C. Martin (Uncle Bob), coined by Michael Feathers (2004)
> **Reference:** [Clean Code](https://blog.cleancoder.com/uncle-bob/2020/10/18/Solid-Relevance.html)

| Principle | Description | Example in Project |
|-----------|-------------|-------------------|
| **S**ingle Responsibility | One reason to change | `AgentSession` handles only agent state |
| **O**pen-Closed | Open for extension, closed for modification | Ports enable new adapters without changing domain |
| **L**iskov Substitution | Subtypes replaceable | All adapters implement port interfaces correctly |
| **I**nterface Segregation | No forced dependencies | Narrow ports: `LLMPort`, `EventStorePort`, `WorkerToolPort` |
| **D**ependency Inversion | Depend on abstractions | Core depends on ports, not concrete adapters |

---

## DRY - Don't Repeat Yourself

> **Source:** Andy Hunt & Dave Thomas, *The Pragmatic Programmer* (1999)

**Definition:** "Every piece of knowledge must have a single, unambiguous, authoritative representation."

**Note:** DRY applies to **knowledge duplication**, not just code. Duplicate code representing different knowledge is acceptable.

| Knowledge | Single Location |
|-----------|-----------------|
| Prompt templates | `prompts/` directory (Jinja2) |
| Event application logic | `_apply()` singledispatchmethod |
| Domain events | `core/domain/events.py` |
| Validation rules | Domain aggregates |

---

## Test-Driven Development (Kent Beck)

> **Source:** Kent Beck, *Test-Driven Development: By Example* (2002)
> **Reference:** [Martin Fowler on TDD](https://martinfowler.com/bliki/TestDrivenDevelopment.html)

**Red-Green-Refactor Cycle:**
1. **Red:** Write failing test first
2. **Green:** Write minimal code to pass
3. **Refactor:** Improve design, tests stay green

**Testing Conventions:**
- Given-When-Then (BDD) structure
- Test doubles: `FakeLLM`, `FakeEventStore`
- Verify events, not just state (event sourcing)
- Test locations: `tests/`, `*/tests/` per layer

---

## Domain-Driven Design (Eric Evans)

> **Source:** Eric Evans, *Domain-Driven Design* (2003)
> **Reference:** [DDD Reference](https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf)

| Concept | Description | Example |
|---------|-------------|---------|
| **Entity** | Object with unique identity | `AgentSession` (by `session_id`) |
| **Value Object** | Immutable, defined by attributes | `DomainEvent`, `Subtask` |
| **Aggregate** | Consistency boundary | `AgentSession` is aggregate root |
| **Bounded Context** | Logical model boundary | Multi-agent orchestration domain |

---

## Design by Contract (Bertrand Meyer)

> **Source:** Bertrand Meyer, *Object-Oriented Software Construction* (1988)
> **Reference:** [Design by Contract](https://se.inf.ethz.ch/~meyer/publications/old/dbc_chapter.pdf)

| Element | Description |
|---------|-------------|
| **Preconditions** | Requirements before method execution (caller's responsibility) |
| **Postconditions** | Guarantees after execution (callee's responsibility) |
| **Invariants** | Constraints that always hold for aggregate |

### Contract vs Validation

| Aspect | Contract (DbC) | Validation |
|--------|---------------|------------|
| Trust | Both parties trust contract | Neither trusts the other |
| Location | Domain core (aggregates) | Layer boundaries |
| Enforcement | Assertions (fail fast) | Try-catch, error handling |
| Source | Internal callers (trusted) | External input (untrusted) |

**Use Assertions for:**
- Invariants: "First event must be `AgentCreated`"
- Preconditions: `evaluate_task()` requires MANAGER role

**Use Validation for:**
- External LLM responses (untrusted)
- User input, configuration files

---

## Offensive Programming (Fail Fast)

Code should "fail hard" with assertions as safety net:
- Use assertions for preconditions/invariants
- Let system crash if contracts violated (development)
- Failed assertions = bugs, not runtime conditions
