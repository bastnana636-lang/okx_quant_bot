"""Read and update the active mean-reversion controller YAML files.

The dashboard stays on the Python standard library. These files are flat
``key: value`` documents, so the editor rewrites matching lines and keeps
comments and key order.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(os.environ.get("OKX_TRADER_ROOT", Path(__file__).resolve().parents[1])).resolve()
SCRIPT_FILE = ROOT / "conf" / "scripts" / "conf_okx_multi.yml"
CONTROLLERS_DIR = ROOT / "conf" / "controllers"

FILE_NAME_RE = re.compile(r"^conf_okx_pmm_[a-z0-9]+\.yml$")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")
INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h")
STRATEGY_INFO = {
    "key": "standard_5m",
    "name": "标准均值回归 (5m)",
    "badge": "5m 均值回归策略",
    "interval": "5m",
    "mean_window": 48,
    "time_limit": 3600,
    "description": "5分钟 K 线，4小时均值窗口，持仓时限 1 小时，动态 ATR 止损 0.6%~2%，稳健波段均值修复。",
}


# (key, label, kind, bounds)
# kind: int | decimal | choice | bool
# bounds: (lo, hi) for int, dict of gt/ge/lt/le for decimal, choices for choice
PAIR_FIELDS = (
    ("leverage", "杠杆", "int", (1, 5)),
    ("total_amount_quote", "开单金额 USDT", "decimal", {"gt": 0}),
    ("take_profit_quote", "止盈 USDT", "decimal", {"gt": 0}),
    ("fixed_unrealized_tp_quote", "固定浮盈平仓 USDT", "decimal", {"gt": 1}),
)
SHARED_FIELDS = (
    ("candles_interval", "K 线周期", "choice", INTERVALS),
    ("mean_window", "均值窗口", "int", (10, 500)),
    ("entry_z_score", "入场 Z", "decimal", {"gt": 0}),
    ("max_entry_z_score", "最大入场 Z", "decimal", {"gt": 0}),
    ("exit_z_score", "出场 Z", "decimal", {"ge": 0}),
    ("min_std_pct", "最小波动率", "decimal", {"gt": 0, "lt": 1}),
    ("atr_length", "ATR 长度", "int", (2, 100)),
    ("atr_stop_multiplier", "ATR 止损倍数", "decimal", {"gt": 0}),
    ("min_stop_loss", "止损下限", "decimal", {"gt": 0, "lt": 1}),
    ("max_stop_loss", "止损上限", "decimal", {"gt": 0, "lt": 1}),
    ("round_trip_cost_pct", "往返成本", "decimal", {"ge": 0, "lt": 1}),
    ("min_net_edge_pct", "最小净边际", "decimal", {"gt": 0, "lt": 1}),
    ("min_reward_risk", "最小收益风险比", "decimal", {"gt": 0}),
    ("quote_offset_pct", "报价偏移", "decimal", {"ge": 0, "lt": 1}),
    ("max_bid_ask_spread_pct", "最大点差", "decimal", {"gt": 0, "lt": 1}),
    ("executor_refresh_time", "挂单刷新秒", "int", (1, None)),
    ("cooldown_time", "交易冷静期秒", "int", (1, None)),
    ("stop_loss_cooldown_time", "止损冷静期秒", "int", (1, None)),
    ("time_limit", "持仓时限秒", "int", (60, None)),
    ("manual_kill_switch", "手动停机", "bool", None),
)
PAIR_KEYS = {item[0] for item in PAIR_FIELDS}
SHARED_KEYS = {item[0] for item in SHARED_FIELDS}
EDITABLE_KEYS = PAIR_KEYS | SHARED_KEYS


class ConfigError(ValueError):
    """User-facing configuration error."""


def active_controller_names(script_file: Path = SCRIPT_FILE) -> list[str]:
    if not script_file.is_file():
        raise ConfigError(f"找不到策略清单 {script_file}")
    names = []
    for line in script_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped.endswith(".yml"):
            name = stripped[2:].strip()
            if not FILE_NAME_RE.fullmatch(name):
                raise ConfigError(f"策略清单包含不支持的控制器文件 {name}")
            names.append(name)
    if not names:
        raise ConfigError("策略清单里没有控制器")
    if len(names) != len(set(names)):
        raise ConfigError("策略清单里有重复的控制器")
    return names


def parse_flat_yaml(text: str) -> dict[str, str]:
    data = {}
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and not line.lstrip().startswith("#"):
            data[match.group(1)] = match.group(2)
    return data


def render_updates(text: str, updates: dict[str, str]) -> str:
    remaining = dict(updates)
    lines = []
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and match.group(1) in remaining and not line.lstrip().startswith("#"):
            key = match.group(1)
            lines.append(f"{key}: {remaining.pop(key)}")
        else:
            lines.append(line)
    for key, value in remaining.items():
        lines.append(f"{key}: {value}")
    body = "\n".join(lines)
    if text.endswith("\n") or not text:
        body += "\n"
    return body


def load_config(script_file: Path = SCRIPT_FILE, controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    names = active_controller_names(script_file)
    parsed = []
    for name in names:
        path = controllers_dir / name
        if not path.is_file():
            raise ConfigError(f"找不到控制器配置 {name}")
        raw = parse_flat_yaml(path.read_text(encoding="utf-8"))
        required = (EDITABLE_KEYS - {"fixed_unrealized_tp_quote"}) | {"trading_pair"}
        missing = [key for key in required if key not in raw]
        if missing:
            raise ConfigError(f"{name} 缺少字段: {', '.join(missing)}")
        raw.setdefault("fixed_unrealized_tp_quote", "2")
        parsed.append((name, raw))

    shared = {key: parsed[0][1][key] for key in SHARED_KEYS}
    mixed = {key: False for key in SHARED_KEYS}
    for _, raw in parsed[1:]:
        for key in SHARED_KEYS:
            if raw[key] != shared[key]:
                mixed[key] = True

    pairs = []
    for name, raw in parsed:
        pairs.append({
            "file": name,
            "trading_pair": raw["trading_pair"],
            "leverage": raw["leverage"],
            "total_amount_quote": raw["total_amount_quote"],
            "take_profit_quote": raw["take_profit_quote"],
            "fixed_unrealized_tp_quote": raw.get("fixed_unrealized_tp_quote", "2"),
        })
    return {
        "pairs": pairs,
        "shared": shared,
        "shared_mixed": mixed,
        "defaults": {"leverage": 3},
        "strategy": STRATEGY_INFO,
    }



def apply_config(payload: dict, script_file: Path = SCRIPT_FILE,
                 controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是 JSON 对象")
    current_names = active_controller_names(script_file)
    pairs = payload.get("pairs")
    shared = payload.get("shared")
    if not isinstance(pairs, list) or not isinstance(shared, dict):
        raise ConfigError("需要 pairs 和 shared")
    incoming = []
    for item in pairs:
        if not isinstance(item, dict):
            raise ConfigError("pairs 中的每一项都必须是对象")
        name = str(item.get("file", ""))
        if name not in current_names or not FILE_NAME_RE.fullmatch(name):
            raise ConfigError(f"未知的控制器文件 {name}")
        incoming.append(name)
    if sorted(incoming) != sorted(current_names):
        raise ConfigError("必须同时提交当前已启用的全部币种")

    originals = {
        name: parse_flat_yaml((controllers_dir / name).read_text(encoding="utf-8"))
        for name in current_names
    }
    shared_values = _normalize_fields(shared, SHARED_FIELDS)
    normalized_pairs = []
    for item in pairs:
        pair_values = _normalize_fields(item, PAIR_FIELDS)
        merged = {**shared_values, **pair_values}
        _validate_relationships(item["file"], merged)
        normalized_pairs.append((item["file"], pair_values))

    written = []
    for name, pair_values in normalized_pairs:
        path = controllers_dir / name
        original_text = path.read_text(encoding="utf-8")
        updates = {
            key: _preserve_spelling(originals[name].get(key), value)
            for key, value in {**shared_values, **pair_values}.items()
        }
        updated = render_updates(original_text, updates)
        if updated != original_text:
            _atomic_write(path, updated)
        written.append(name)
    return {"files": written}


def _preserve_spelling(previous: str | None, normalized: str) -> str:
    if previous is None:
        return normalized
    if previous == normalized:
        return previous
    try:
        if Decimal(previous) == Decimal(normalized):
            return previous
    except (InvalidOperation, ValueError):
        pass
    return normalized


def _normalize_fields(source: dict, specs) -> dict[str, str]:
    values = {}
    for key, label, kind, bounds in specs:
        if key not in source:
            if key == "fixed_unrealized_tp_quote":
                source[key] = "2"
            else:
                raise ConfigError(f"缺少 {label}")
        values[key] = _normalize_value(label, source[key], kind, bounds)
    return values


def _normalize_value(label: str, raw, kind: str, bounds) -> str:
    text = str(raw).strip()
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"true", "yes", "1"}:
            return "true"
        if lowered in {"false", "no", "0"}:
            return "false"
        raise ConfigError(f"{label} 只能是 true 或 false")
    if kind == "choice":
        if text not in bounds:
            raise ConfigError(f"{label} 只能是 {', '.join(bounds)}")
        return text
    if kind == "int":
        if not re.fullmatch(r"-?\d+", text):
            raise ConfigError(f"{label} 必须是整数")
        value = int(text)
        lo, hi = bounds
        if lo is not None and value < lo:
            raise ConfigError(f"{label} 不能小于 {lo}")
        if hi is not None and value > hi:
            raise ConfigError(f"{label} 不能大于 {hi}")
        return str(value)
    if kind == "decimal":
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            raise ConfigError(f"{label} 不是有效数字")
        if not value.is_finite():
            raise ConfigError(f"{label} 不是有效数字")
        for op, limit in bounds.items():
            limit = Decimal(str(limit))
            if op == "gt" and not value > limit:
                raise ConfigError(f"{label} 必须大于 {limit}")
            if op == "ge" and not value >= limit:
                raise ConfigError(f"{label} 不能小于 {limit}")
            if op == "lt" and not value < limit:
                raise ConfigError(f"{label} 必须小于 {limit}")
            if op == "le" and not value <= limit:
                raise ConfigError(f"{label} 不能大于 {limit}")
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    raise ConfigError(f"未知字段类型 {kind}")


def _validate_relationships(name: str, values: dict[str, str]) -> None:
    exit_z = Decimal(values["exit_z_score"])
    entry_z = Decimal(values["entry_z_score"])
    max_z = Decimal(values["max_entry_z_score"])
    if not exit_z < entry_z < max_z:
        raise ConfigError(f"{name}: 需要 出场 Z < 入场 Z < 最大入场 Z")
    if Decimal(values["min_stop_loss"]) > Decimal(values["max_stop_loss"]):
        raise ConfigError(f"{name}: 止损下限不能大于止损上限")
    if int(values["stop_loss_cooldown_time"]) < int(values["cooldown_time"]):
        raise ConfigError(f"{name}: 止损冷静期不能短于交易冷静期")
    if int(values["executor_refresh_time"]) >= int(values["time_limit"]):
        raise ConfigError(f"{name}: 挂单刷新必须短于持仓时限")


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def get_strategy_info(controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    """Return active strategy metadata."""
    return STRATEGY_INFO


def get_presets_info(controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    """Compatibility wrapper returning current active strategy info."""
    return {
        "active_preset": "standard_5m",
        "active_meta": STRATEGY_INFO,
        "presets": [STRATEGY_INFO],
    }

