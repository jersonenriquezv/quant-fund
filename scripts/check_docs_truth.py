#!/usr/bin/env python3
"""Check high-impact documentation facts against code.

This is intentionally narrow. It protects the facts that can create bad
operational decisions if stale: active setup status, ML feature version,
selected strategy/risk constants, and schema migration version.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.py"
DATA_STORE_PATH = ROOT / "data_service" / "data_store.py"
METADATA_PATH = ROOT / "data_service" / "metadata.py"
WEBSOCKET_PATH = ROOT / "data_service" / "websocket_feeds.py"
BASELINE_PATH = ROOT / "docs" / "SYSTEM_BASELINE.md"
OPERATIONS_PATH = ROOT / "docs" / "OPERATIONS.md"
DATA_SERVICE_DOC_PATH = ROOT / "docs" / "context" / "01-data-service.md"


SETUP_LABELS = {
    "setup_a": "A",
    "setup_b": "B",
    "setup_c": "C",
    "setup_d_choch": "D_choch",
    "setup_d_bos": "D_bos",
    "setup_e": "E",
    "setup_f": "F",
    "setup_g": "G",
    "setup_h": "H",
}


RISK_DOC_PATH = ROOT / "docs" / "context" / "04-risk.md"
AI_DOC_PATH = ROOT / "docs" / "context" / "03-ai-filter.md"

# Modules that must have a sub-CLAUDE.md governing edit rules.
# Add new modules here as their sub-CLAUDE.md is created.
SUB_CLAUDEMD_REQUIRED = (
    "strategy_service",
    "risk_service",
    "execution_service",
    "data_service",
    "ai_service",
    "dashboard",
)

MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-jer-quant-fund" / "memory"

# Settings whose name appears in SYSTEM_BASELINE but live elsewhere
# (env-only, dynamic, removed, or grouped under another constant).
# These names are skipped by check_baseline_settings_exist.
BASELINE_SETTING_EXEMPT = {
    "TP1_RR_RATIO",
    "SETUP_TP2_RR",
    "SHADOW_FEAR_LONG_GATE",
    "SHADOW_MIN_HOUR_UTC",
    "SETUP_H_",
    "ENTRY_TIMEOUT",
    "MAX_TRADE_DURATION",
    "TRAILING_TP_ENABLED",
    "QUICK_OB_MAX_DISTANCE_PCT",
    "SETUP_D_ENTRY_PCT",
    "SETUP_A_MAX_SWEEP_CHOCH_GAP",
    "SHADOW_DEDUP_TTL",
    "TRADING_PAIRS",
    "HTF_TIMEFRAMES",
    "LTF_TIMEFRAMES",
    "SWING_SETUP_TIMEFRAMES",
}

# Constants whose doc table value must exactly match settings.py.
# Format: setting name → expected doc representation (plain = str(value)).
BASELINE_CONSTANTS = {
    "ATR_SL_FLOOR_MULTIPLIER": "plain",
    "REGIME_EXTREME_FEAR_GATE": "plain",
    "SETUP_F_MAX_BOS_AGE_CANDLES": "plain",
    "MAX_LEVERAGE": "plain",
    "MAX_OPEN_POSITIONS": "plain",
    "MAX_TRADES_PER_DAY": "plain",
    "COOLDOWN_MINUTES": "plain",
    "MIN_RISK_REWARD": "plain",
    "MIN_RISK_REWARD_QUICK": "plain",
}

# Setups that have been removed and must show DISABLED in docs.
REMOVED_SETUPS = {"setup_c", "setup_e", "setup_h"}


def _eval_node(node: ast.AST):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_eval_node(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {_eval_node(k): _eval_node(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Lambda):
        return _eval_node(node.body)
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in {"int", "float", "str"} and node.args:
            builtin = {"int": int, "float": float, "str": str}[name]
            return builtin(_eval_node(node.args[0]))
        if name == "os.getenv":
            if len(node.args) >= 2:
                return _eval_node(node.args[1])
            return ""
        if name == "field":
            for keyword in node.keywords:
                if keyword.arg == "default_factory":
                    return _eval_node(keyword.value)
    raise ValueError(f"Unsupported settings expression: {ast.dump(node)}")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def load_settings_defaults() -> dict[str, object]:
    tree = ast.parse(SETTINGS_PATH.read_text())
    values: dict[str, object] = {}
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == "Settings":
            for stmt in item.body:
                target = None
                value = None
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target = stmt.target.id
                    value = stmt.value
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target = stmt.targets[0].id
                    value = stmt.value
                if target and value is not None:
                    try:
                        values[target] = _eval_node(value)
                    except Exception:
                        # Cannot evaluate (complex expression like getenv().lower() == "true")
                        # but record name as a sentinel so existence checks still pass.
                        values.setdefault(target, _UNEVALUATED)
    return values


class _Unevaluated:
    """Sentinel for settings whose value AST eval cannot resolve."""

    def __repr__(self) -> str:
        return "<unevaluated>"


_UNEVALUATED = _Unevaluated()


def latest_schema_version() -> int:
    text = DATA_STORE_PATH.read_text()
    versions = [int(match) for match in re.findall(r"_apply_migration\([^,]+,\s*(\d+),", text)]
    if not versions:
        raise RuntimeError("No schema migrations found in data_store.py")
    return max(versions)


def load_module_literal(name: str, path: Path) -> object:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} not found in {path}")


def line_number(text: str, needle: str) -> int:
    index = text.find(needle)
    if index < 0:
        return 1
    return text[:index].count("\n") + 1


def add_issue(issues: list[str], path: Path, text: str, needle: str, message: str) -> None:
    rel = path.relative_to(ROOT)
    issues.append(f"{rel}:{line_number(text, needle)}: {message}")


def parse_setup_rows(baseline: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    section = baseline
    start = baseline.find("### Setup Status")
    end = baseline.find("### Risk Guardrails", start)
    if start >= 0 and end > start:
        section = baseline[start:end]
    for raw in section.splitlines():
        if not raw.startswith("|"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0].split(" ", 1)[0]
        rows[label] = cells[1].upper()
    return rows


def expected_setup_status(setup: str, enabled: set[str], shadow: set[str]) -> str:
    if setup in shadow:
        return "SHADOW"
    if setup in enabled:
        return "LIVE"
    return "DISABLED"


def check_baseline(settings: dict[str, object], issues: list[str]) -> None:
    baseline = BASELINE_PATH.read_text()

    ml_version = int(settings["ML_FEATURE_VERSION"])
    header = re.search(r"\*\*ML Feature Version:\*\*\s*(\d+)", baseline)
    if not header or int(header.group(1)) != ml_version:
        add_issue(
            issues,
            BASELINE_PATH,
            baseline,
            "**ML Feature Version:**",
            f"ML Feature Version must be {ml_version}",
        )

    current = re.search(r"\*\*Current version:\*\*\s*(\d+)", baseline)
    if not current or int(current.group(1)) != ml_version:
        add_issue(
            issues,
            BASELINE_PATH,
            baseline,
            "**Current version:**",
            f"ML feature current version must be {ml_version}",
        )

    enabled = set(settings.get("ENABLED_SETUPS", []))
    shadow = set(settings.get("SHADOW_MODE_SETUPS", []))
    rows = parse_setup_rows(baseline)
    for setup, label in SETUP_LABELS.items():
        expected = expected_setup_status(setup, enabled, shadow)
        status = rows.get(label)
        if status is None:
            add_issue(issues, BASELINE_PATH, baseline, "### Setup Status", f"Missing setup row for {label}")
            continue
        if expected == "SHADOW" and "SHADOW" not in status:
            add_issue(issues, BASELINE_PATH, baseline, f"| {label}", f"{label} status must include SHADOW")
        elif expected == "LIVE" and not ("LIVE" in status or "ENABLED" in status):
            add_issue(issues, BASELINE_PATH, baseline, f"| {label}", f"{label} status must be LIVE/ENABLED")
        elif expected == "DISABLED" and "DISABLED" not in status:
            add_issue(issues, BASELINE_PATH, baseline, f"| {label}", f"{label} status must be DISABLED")

    for name in BASELINE_CONSTANTS:
        expected = str(settings[name])
        row = re.search(rf"\|\s*{re.escape(name)}\s*\|\s*([^|]+?)\s*\|", baseline)
        if row:
            # Strip common suffixes (x, %, min) for comparison
            doc_val = re.sub(r'[x%]$', '', row.group(1).strip())
            if doc_val != expected:
                add_issue(issues, BASELINE_PATH, baseline, name, f"{name} must be documented as {expected} (got {row.group(1).strip()})")
        else:
            add_issue(issues, BASELINE_PATH, baseline, name, f"{name} must have a row in SYSTEM_BASELINE")

    atr = str(settings["ATR_SL_FLOOR_MULTIPLIER"])
    if f"{atr}× ATR" not in baseline and f"{atr}x ATR" not in baseline:
        add_issue(
            issues,
            BASELINE_PATH,
            baseline,
            "ATR SL floor",
            f"Pipeline/gating text must mention {atr}× ATR (or {atr}x ATR), not a stale multiplier",
        )

    # Funding threshold: must reflect 3-tier system, not old single 0.0003
    funding_extreme = settings.get("FUNDING_EXTREME_THRESHOLD")
    if funding_extreme is not None:
        fe_str = str(funding_extreme)
        if f"FUNDING_EXTREME_THRESHOLD" in baseline:
            row = re.search(r"\|\s*FUNDING_EXTREME_THRESHOLD\s*\|\s*([^|]+?)\s*\|", baseline)
            if row and fe_str not in row.group(1):
                add_issue(issues, BASELINE_PATH, baseline, "FUNDING_EXTREME_THRESHOLD",
                          f"FUNDING_EXTREME_THRESHOLD must be {fe_str} (3-tier system)")

    # TP2_RR table must not list removed setups
    tp2_row = re.search(r"\|\s*SETUP_TP2_RR\s*\|\s*([^|]+?)\s*\|", baseline)
    if tp2_row:
        tp2_text = tp2_row.group(1)
        for removed in ["C/E", "H=", "/H="]:
            if removed in tp2_text and "removed" not in tp2_text.lower():
                add_issue(issues, BASELINE_PATH, baseline, "SETUP_TP2_RR",
                          f"SETUP_TP2_RR table lists removed setups ({removed})")

    # Removed setups must be DISABLED
    for setup in REMOVED_SETUPS:
        label = SETUP_LABELS.get(setup)
        if label and label in rows:
            status = rows[label]
            if "DISABLED" not in status:
                add_issue(issues, BASELINE_PATH, baseline, f"| {label}",
                          f"Removed setup {label} must be DISABLED, got {status}")


def check_operations(issues: list[str]) -> None:
    operations = OPERATIONS_PATH.read_text()
    latest = latest_schema_version()
    current = re.search(r"### Current Schema Version:\s*(\d+)", operations)
    if not current or int(current.group(1)) != latest:
        add_issue(
            issues,
            OPERATIONS_PATH,
            operations,
            "### Current Schema Version:",
            f"Current schema version must be {latest}",
        )
    if not re.search(rf"\|\s*{latest}\s*\|", operations):
        add_issue(
            issues,
            OPERATIONS_PATH,
            operations,
            "| Version |",
            f"Schema migration table must include version {latest}",
        )


def check_data_service_docs(settings: dict[str, object], issues: list[str]) -> None:
    doc = DATA_SERVICE_DOC_PATH.read_text()
    pairs = list(settings.get("TRADING_PAIRS", []))
    instruments = load_module_literal("OKX_SWAP_INSTRUMENTS", METADATA_PATH)
    contract_sizes = load_module_literal("CONTRACT_SIZES", METADATA_PATH)
    timeframes = load_module_literal("_TIMEFRAME_TO_CHANNEL", WEBSOCKET_PATH)

    missing_inst = [p for p in pairs if p not in instruments]
    missing_contract = [p for p in pairs if p not in contract_sizes]
    if missing_inst:
        add_issue(issues, METADATA_PATH, METADATA_PATH.read_text(), "OKX_SWAP_INSTRUMENTS", f"Missing OKX instrument metadata for {missing_inst}")
    if missing_contract:
        add_issue(issues, METADATA_PATH, METADATA_PATH.read_text(), "CONTRACT_SIZES", f"Missing contract size metadata for {missing_contract}")

    expected_channels = len(pairs) * len(timeframes)
    expected_phrase = f"{expected_channels} total: {len(pairs)} pairs × {len(timeframes)} timeframes"
    if expected_phrase not in doc:
        add_issue(
            issues,
            DATA_SERVICE_DOC_PATH,
            doc,
            "Connect OKX WebSocket",
            f"Data Service pipeline flow must mention {expected_phrase}",
        )

    stale_patterns = [
        "4 pairs",
        "16 channels",
        "CryptoCompare news API endpoint (free, no key)",
        "Supported pairs: BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP",
        "| `TRADE_CAPITAL_PCT` |",
        "| `CVD_WARMUP_SECONDS` |",
    ]
    for pattern in stale_patterns:
        if pattern in doc:
            add_issue(
                issues,
                DATA_SERVICE_DOC_PATH,
                doc,
                pattern,
                f"Stale Data Service doc text remains: {pattern}",
            )

    required_patterns = [
        "data_service/metadata.py",
        "CVDSnapshot` carries `warm_windows`",
        "liquidation_estimator.py",
    ]
    for pattern in required_patterns:
        if pattern not in doc:
            add_issue(
                issues,
                DATA_SERVICE_DOC_PATH,
                doc,
                "Data Service",
                f"Data Service docs must mention {pattern}",
            )


def check_risk_docs(settings_vals: dict[str, object], issues: list[str]) -> None:
    """Verify risk service doc guardrail count and key thresholds."""
    if not RISK_DOC_PATH.exists():
        return
    doc = RISK_DOC_PATH.read_text()

    # Guardrail count — code has 9 (3 structural + 5 state + 1 post-sizing)
    if "6 checks" in doc:
        add_issue(issues, RISK_DOC_PATH, doc, "6 checks",
                  "Guardrail count is 9, not 6 (3 structural + 5 state + 1 post-sizing)")
    # check_max_sl_distance must be mentioned
    if "check_max_sl_distance" not in doc:
        add_issue(issues, RISK_DOC_PATH, doc, "guardrails",
                  "Missing check_max_sl_distance guardrail in risk docs (MAX_SL_PCT cap)")


def check_ai_docs(settings_vals: dict[str, object], issues: list[str]) -> None:
    """Verify AI filter doc thresholds."""
    if not AI_DOC_PATH.exists():
        return
    doc = AI_DOC_PATH.read_text()

    fear_threshold = settings_vals.get("NEWS_EXTREME_FEAR_THRESHOLD")
    if fear_threshold is not None:
        # Check that the doc has the correct value
        fear_match = re.search(r"NEWS_EXTREME_FEAR_THRESHOLD.*?\((\d+)\)", doc)
        if fear_match and int(fear_match.group(1)) != int(fear_threshold):
            add_issue(issues, AI_DOC_PATH, doc, "NEWS_EXTREME_FEAR_THRESHOLD",
                      f"NEWS_EXTREME_FEAR_THRESHOLD must be {fear_threshold}")


def check_wallet_counts(settings_vals: dict[str, object], issues: list[str]) -> None:
    """Verify whale wallet counts in data service doc."""
    if not DATA_SERVICE_DOC_PATH.exists():
        return
    doc = DATA_SERVICE_DOC_PATH.read_text()

    eth_wallets = settings_vals.get("WHALE_WALLETS", {})
    btc_wallets = settings_vals.get("BTC_WHALE_WALLETS", {})
    if isinstance(eth_wallets, dict):
        eth_count = len(eth_wallets)
        eth_match = re.search(r"Polls (\d+) configured (?:ETH )?wallets", doc)
        if eth_match and int(eth_match.group(1)) != eth_count:
            add_issue(issues, DATA_SERVICE_DOC_PATH, doc, "Polls",
                      f"ETH wallet count must be {eth_count}, doc says {eth_match.group(1)}")
    if isinstance(btc_wallets, dict):
        btc_count = len(btc_wallets)
        btc_match = re.search(r"Polls (\d+) configured (?:BTC )?wallets", doc)
        if btc_match and int(btc_match.group(1)) != btc_count:
            add_issue(issues, DATA_SERVICE_DOC_PATH, doc, "BTC",
                      f"BTC wallet count must be {btc_count}, doc says {btc_match.group(1)}")


def check_sub_claudemd(issues: list[str]) -> None:
    """Each module in SUB_CLAUDEMD_REQUIRED must have a CLAUDE.md."""
    for module in SUB_CLAUDEMD_REQUIRED:
        module_dir = ROOT / module
        if not module_dir.exists():
            continue
        sub_doc = module_dir / "CLAUDE.md"
        if not sub_doc.exists():
            issues.append(
                f"{module}/: sub-CLAUDE.md missing — module touched by edit rules but doc absent"
            )


def check_memory_pointers(issues: list[str]) -> None:
    """MEMORY.md must only link to existing files in the memory directory."""
    index = MEMORY_DIR / "MEMORY.md"
    if not index.exists():
        return
    text = index.read_text()
    # Match markdown links `[Title](file.md)` — only relative .md links
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", text):
        target = match.group(1)
        # Skip absolute or http links
        if target.startswith(("http://", "https://", "/")):
            continue
        target_path = MEMORY_DIR / target
        if not target_path.exists():
            line_no = text[: match.start()].count("\n") + 1
            issues.append(f"{index.relative_to(Path.home())}:{line_no}: dead link → {target}")


def check_baseline_settings_exist(settings: dict[str, object], issues: list[str]) -> None:
    """Every UPPER_CASE setting name in SYSTEM_BASELINE §1 must exist in settings.py."""
    baseline = BASELINE_PATH.read_text()
    section_start = baseline.find("## 1. Active Configuration")
    section_end = baseline.find("## 2.", section_start) if section_start >= 0 else -1
    if section_start < 0 or section_end < 0:
        return
    section = baseline[section_start:section_end]

    seen: set[str] = set()
    # Match table rows: `| SETTING_NAME | value | ...`
    for match in re.finditer(r"\|\s*([A-Z][A-Z0-9_]{3,})\s*\|", section):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        # Skip exempt names and table headers
        if name in BASELINE_SETTING_EXEMPT or name in {"PARAMETER", "VALUE", "NOTES", "SOURCE", "SETUP"}:
            continue
        if any(name.startswith(prefix) for prefix in ("PARAMETER", "VALUE")):
            continue
        if name not in settings:
            line_no = baseline[: section_start + match.start()].count("\n") + 1
            issues.append(
                f"{BASELINE_PATH.relative_to(ROOT)}:{line_no}: "
                f"'{name}' documented but not found in config/settings.py "
                f"(remove from doc or add to BASELINE_SETTING_EXEMPT)"
            )


def main() -> int:
    issues: list[str] = []
    settings = load_settings_defaults()
    check_baseline(settings, issues)
    check_operations(issues)
    check_data_service_docs(settings, issues)
    check_risk_docs(settings, issues)
    check_ai_docs(settings, issues)
    check_wallet_counts(settings, issues)
    check_sub_claudemd(issues)
    check_memory_pointers(issues)
    check_baseline_settings_exist(settings, issues)

    if issues:
        print("Docs truth check failed:")
        for issue in issues:
            print(f"- {issue}")
        print("\nRun /doc-audit or update docs to match code.")
        return 1

    print("Docs truth check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
