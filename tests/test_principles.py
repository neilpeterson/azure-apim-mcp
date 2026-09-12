"""AST/grep-based enforcement of the rules in docs/PRINCIPLES.md.

Every test here scans ``src/`` itself, so it passes trivially against an
(almost) empty package today. That is intentional — the point is that the
*first* violation, whenever it is introduced, fails loudly in CI rather than
being discovered in review. Do not weaken these into no-ops just to make a
future task's code pass; fix the code instead.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "apim_mcp"
INFRA_ROOT = Path(__file__).resolve().parent.parent / "infra"

# Principle 1: credential classes that must only be constructed in the seam.
CREDENTIAL_CLASS_NAMES = frozenset(
    {
        "DefaultAzureCredential",
        "ManagedIdentityCredential",
        "ClientSecretCredential",
        "ClientCertificateCredential",
        "EnvironmentCredential",
        "ChainedTokenCredential",
        "AzureCliCredential",
        "OnBehalfOfCredential",
        "WorkloadIdentityCredential",
        "InteractiveBrowserCredential",
        "DeviceCodeCredential",
    }
)
ALLOWED_CREDENTIAL_MODULE = SRC_ROOT / "auth" / "credentials.py"

# Principle 3: cache decorators that must be keyed by oid.
CACHE_DECORATOR_NAMES = frozenset({"lru_cache", "cache", "ttl_cache"})
# Principle 3 exception: the shared API index is deliberately unkeyed.
CACHE_OID_ALLOWLIST = (SRC_ROOT / "index",)

# Principle 5: secret-retrieving ARM/APIM actions that must never be called.
FORBIDDEN_SECRET_ACTIONS = (
    "listSecrets",
    "listValue",
    "listKeys",
    "users/token",
)

# Principle 7: SDK/HTTP clients that must be constructed per request, not
# held as module-level or startup singletons.
FORBIDDEN_SINGLETON_CLASSES = frozenset(
    {
        "ApiManagementClient",
        "LogsQueryClient",
        "MetricsQueryClient",
        "ArmClient",
    }
)
# httpx.AsyncClient is allowed as a module-level singleton only when it
# carries no identity (the SAS-signed spec export blob fetch).
AUTH_CARRYING_CLIENT_NAMES = frozenset({"AsyncClient"})


def _iter_source_files(root: Path = SRC_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.expr) -> str | None:
    """Return the simple name of a call target, e.g. ``Credential`` from
    ``foo.Credential(...)`` or plain ``Credential(...)``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def test_no_direct_credential_construction() -> None:
    """Principle 1: only auth/credentials.py may construct a credential."""
    violations: list[Violation] = []
    for path in _iter_source_files():
        if path == ALLOWED_CREDENTIAL_MODULE:
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name in CREDENTIAL_CLASS_NAMES:
                violations.append(Violation(path, node.lineno, f"direct construction of {name}"))
    assert not violations, "credential constructed outside the seam:\n" + "\n".join(
        str(v) for v in violations
    )


def _decorator_names(decorators: list[ast.expr]) -> list[str]:
    names = []
    for dec in decorators:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _call_name(target)
        if name:
            names.append(name)
    return names


def _function_param_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = func.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def test_cache_keys_include_oid() -> None:
    """Principle 3: cache decorators must key on an oid-derived parameter."""
    violations: list[Violation] = []
    for path in _iter_source_files():
        if any(path.is_relative_to(allowed) for allowed in CACHE_OID_ALLOWLIST):
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not set(_decorator_names(node.decorator_list)) & CACHE_DECORATOR_NAMES:
                continue
            params = _function_param_names(node)
            if not any("oid" in p.lower() for p in params):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        f"cached function {node.name!r} has no oid-derived parameter",
                    )
                )
    assert not violations, "cache not keyed by oid:\n" + "\n".join(str(v) for v in violations)


def _iter_registered_tools() -> list[dict[str, object]]:
    """Best-effort discovery of registered MCP tools and their annotations.

    Looks for ``@mcp.tool(...)``-style decorators in src/apim_mcp/tools/ and
    returns the keyword arguments passed to each. Returns an empty list
    (against which the readonly assertion holds vacuously) until the tools
    package and a real registry exist.
    """
    tools_dir = SRC_ROOT / "tools"
    if not tools_dir.is_dir():
        return []
    tools: list[dict[str, object]] = []
    for path in sorted(tools_dir.rglob("*.py")):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                name = _call_name(dec.func)
                if name != "tool":
                    continue
                kwargs: dict[str, object] = {}
                for kw in dec.keywords:
                    if kw.arg is not None and isinstance(kw.value, ast.Constant):
                        kwargs[kw.arg] = kw.value.value
                tools.append(kwargs)
    return tools


def test_all_tools_are_readonly() -> None:
    """Principle 4: every registered tool must carry the read-only hints.

    Stub against an empty registry for now — passes vacuously until Group A
    tools exist, at which point every registered tool must set these three
    annotations.
    """
    violations: list[str] = []
    for tool in _iter_registered_tools():
        if tool.get("readOnlyHint") is not True:
            violations.append(f"{tool}: readOnlyHint must be True")
        if tool.get("destructiveHint") is not False:
            violations.append(f"{tool}: destructiveHint must be False")
        if tool.get("idempotentHint") is not True:
            violations.append(f"{tool}: idempotentHint must be True")
    assert not violations, "non-read-only tool annotation:\n" + "\n".join(violations)


def test_no_secret_actions() -> None:
    """Principle 5: never call a secret-retrieving ARM/APIM action."""
    violations: list[Violation] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for action in FORBIDDEN_SECRET_ACTIONS:
                if action in line:
                    violations.append(Violation(path, lineno, f"forbidden action {action!r}"))
    assert not violations, "secret-retrieving action referenced:\n" + "\n".join(
        str(v) for v in violations
    )


def test_infrastructure_does_not_retrieve_secrets() -> None:
    """Principle 5: deployment templates must not retrieve live secret values."""
    violations: list[Violation] = []
    forbidden_calls = (".listKeys(", ".listSecrets(", ".listValue(")
    for path in INFRA_ROOT.rglob("*.bicep"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for call in forbidden_calls:
                if call in line:
                    violations.append(Violation(path, lineno, f"forbidden call {call!r}"))
    assert not violations, "secret-retrieving infrastructure call:\n" + "\n".join(
        str(v) for v in violations
    )


def _module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    return list(tree.body)


def test_no_module_level_azure_clients() -> None:
    """Principle 7: SDK/HTTP clients must be constructed per request."""
    violations: list[Violation] = []
    for path in _iter_source_files():
        tree = _parse(path)
        for stmt in _module_level_statements(tree):
            if not isinstance(stmt, ast.Assign | ast.AnnAssign):
                continue
            value = stmt.value
            if not isinstance(value, ast.Call):
                continue
            name = _call_name(value.func)
            if name in FORBIDDEN_SINGLETON_CLASSES:
                violations.append(Violation(path, stmt.lineno, f"module-level singleton {name}"))
            elif name in AUTH_CARRYING_CLIENT_NAMES:
                has_auth_kwarg = any(kw.arg == "auth" for kw in value.keywords)
                has_positional_arg = bool(value.args)
                if has_auth_kwarg or has_positional_arg:
                    violations.append(
                        Violation(
                            path,
                            stmt.lineno,
                            f"module-level {name} singleton carries auth",
                        )
                    )
    assert not violations, "module-level Azure client singleton:\n" + "\n".join(
        str(v) for v in violations
    )
