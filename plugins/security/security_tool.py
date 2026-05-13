"""Security tool registry for SEC-bench prompt enrichment."""

from pydantic import BaseModel


class SecurityTool(BaseModel):
    """Metadata for a security analysis tool available in SEC-bench containers."""

    model_config = {"frozen": True}

    name: str
    description: str
    commands: list[str]
    applicable_phases: list[str]


SECURITY_TOOL_REGISTRY: dict[str, SecurityTool] = {
    "valgrind": SecurityTool(
        name="Valgrind",
        description=(
            "Dynamic analysis tool for detecting memory management issues "
            "(leaks, use-after-free, buffer overflows), threading errors, "
            "and generating detailed stack traces. Works on compiled binaries "
            "directly - no special compilation needed (debug symbols with -g recommended)."
        ),
        commands=[
            "valgrind --tool=memcheck --leak-check=full --error-exitcode=1 ./target_binary",
            "valgrind --tool=memcheck --track-origins=yes ./target_binary",
            "valgrind --tool=memcheck --error-exitcode=1 ./target_binary < poc_input",
        ],
        applicable_phases=["exploiter", "fixer"],
    ),
    "klee": SecurityTool(
        name="KLEE",
        description=(
            "Symbolic execution engine that automatically generates test inputs "
            "to achieve high code coverage. Operates on LLVM bitcode - compile "
            "with: clang -emit-llvm -c -g -O0 -Xclang -disable-O0-optnone. "
            "Useful for discovering inputs that trigger specific code paths and "
            "vulnerability conditions."
        ),
        commands=[
            "clang -emit-llvm -c -g -O0 -Xclang -disable-O0-optnone target.c -o target.bc",
            "klee --max-time=120 --output-dir=klee-out target.bc",
            "ktest-tool klee-out/test000001.ktest",
        ],
        applicable_phases=["exploiter"],
    ),
}


def get_tools_for_phase(
    phase: str,
    enabled_tools: list[str],
) -> list[SecurityTool]:
    return [
        tool
        for name, tool in SECURITY_TOOL_REGISTRY.items()
        if name in enabled_tools and phase in tool.applicable_phases
    ]
