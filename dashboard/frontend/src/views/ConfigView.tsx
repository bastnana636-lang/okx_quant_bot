import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Eye,
  Plus,
  RotateCcw,
  Save,
  Search,
  Sliders,
  Sparkles,
  Zap,
} from "lucide-react";
import { postAddPair, postSaveConfig } from "../api";
import type { ConfigResponse, FieldSpec, PairConfigEntry, StrategyPreset } from "../types";
import { CoinBadge } from "../components/CoinBadge";
import { Modal } from "../components/ConfirmModal";

interface ConfigViewProps {
  config: ConfigResponse | null;
  onReloadConfig: () => Promise<void>;
  onToast: (type: "ok" | "err" | "info", msg: string) => void;
  onBotRestarted: () => void;
}

interface DraftState {
  shared: Record<string, string>;
  pairs: Array<{
    file: string;
    id: string;
    trading_pair: string;
    enabled: boolean;
    leverage: string;
    total_amount_quote: string;
    overrides: Record<string, string>;
  }>;
}

const GROUPS: Array<{ id: FieldSpec["group"]; title: string; subtitle: string }> = [
  { id: "mode", title: "信号模式与运行开关 · Signal & Mode", subtitle: "切换 Human波段 / 动量 / 均值回归引擎、K线周期与全局开关" },
  { id: "human", title: "Human 波段与行情研判 · Human Swing & Regime", subtitle: "4H结构共振、BTC联动、11币种广度、自适应追踪止盈与并发风控" },
  { id: "momentum", title: "动量趋势与风险定仓 · Momentum & Sizing", subtitle: "动量回看周期、入场涨幅阈值与等风险定仓比例 (risk_frac)" },
  { id: "reversion", title: "均值回归与 Z-Score 通道 · Mean Reversion", subtitle: "滚动均值窗口、入场/极值/出场 Z-Score 及最小波动率" },
  { id: "risk", title: "ATR 动态止损与收益门槛 · Risk & ATR Stop", subtitle: "ATR 止损倍数、硬止损上下限、成本与盈亏比校验" },
  { id: "execution", title: "挂单执行与冷静期控制 · Execution & Cooldowns", subtitle: "报价偏移、最大点差、订单刷新、冷却时间与持仓时限" },
];

function buildDraft(cfg: ConfigResponse): DraftState {
  return {
    shared: { ...cfg.shared },
    pairs: cfg.all_pairs.map((p) => ({
      file: p.file,
      id: p.id,
      trading_pair: p.trading_pair,
      enabled: p.enabled,
      leverage: p.leverage,
      total_amount_quote: p.total_amount_quote,
      overrides: { ...p.overrides },
    })),
  };
}

export const ConfigView: React.FC<ConfigViewProps> = ({
  config,
  onReloadConfig,
  onToast,
  onBotRestarted,
}) => {
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [paramQuery, setParamQuery] = useState("");
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    mode: true,
    human: true,
    momentum: true,
    reversion: false,
    risk: true,
    execution: true,
  });
  const [batchLev, setBatchLev] = useState("10");
  const [batchAmount, setBatchAmount] = useState("25000");

  const [overrideFile, setOverrideFile] = useState<string | null>(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const [addPairOpen, setAddPairOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  // Add Pair Form
  const [newPairName, setNewPairName] = useState("");
  const [newPairLev, setNewPairLev] = useState("3");
  const [newPairNotional, setNewPairNotional] = useState("50");
  const [newPairEnable, setNewPairEnable] = useState(true);
  const [addingPair, setAddingPair] = useState(false);

  useEffect(() => {
    if (config) {
      setDraft(buildDraft(config));
    }
  }, [config]);

  const sharedSpecs = useMemo(
    () => (config?.field_specs || []).filter((f) => f.group !== "pair"),
    [config]
  );

  // Compute exact diffs against initial config
  const diffs = useMemo(() => {
    if (!config || !draft) return [] as Array<{ target: string; field: string; from: string; to: string }>;
    const list: Array<{ target: string; field: string; from: string; to: string }> = [];
    const origByFile = new Map<string, PairConfigEntry>(config.all_pairs.map((p) => [p.file, p]));

    for (const [k, newVal] of Object.entries(draft.shared)) {
      const oldVal = config.shared[k] ?? "";
      if (newVal !== oldVal) {
        list.push({ target: "全局 Shared", field: k, from: oldVal, to: newVal });
      }
    }

    for (const p of draft.pairs) {
      const orig = origByFile.get(p.file);
      if (!orig) continue;
      if (p.enabled !== orig.enabled) {
        list.push({
          target: p.trading_pair,
          field: "enabled",
          from: orig.enabled ? "启用" : "停用",
          to: p.enabled ? "启用" : "停用",
        });
      }
      for (const pk of ["leverage", "total_amount_quote"] as const) {
        if (p[pk] !== (orig[pk] || "")) {
          list.push({
            target: p.trading_pair,
            field: pk,
            from: orig[pk] || "null",
            to: p[pk] || "null",
          });
        }
      }
      const allOvKeys = new Set([...Object.keys(orig.overrides), ...Object.keys(p.overrides)]);
      for (const ok of allOvKeys) {
        const oldOv = orig.overrides[ok];
        const newOv = p.overrides[ok];
        if (oldOv !== newOv) {
          list.push({
            target: `${p.trading_pair} (覆盖)`,
            field: ok,
            from: oldOv ?? `全局(${config.shared[ok]})`,
            to: newOv ?? `全局(${draft.shared[ok]})`,
          });
        }
      }
    }
    return list;
  }, [config, draft]);

  // Real-time client validation matching strategy_config.py invariants
  const validationErrors = useMemo(() => {
    if (!draft) return [] as string[];
    const errs: string[] = [];
    const enabledPairs = draft.pairs.filter((p) => p.enabled);
    if (enabledPairs.length === 0) {
      errs.push("至少需要启用 1 个交易对控制器。");
    }

    const checkScope = (label: string, values: Record<string, string>) => {
      const exitZ = Number(values.exit_z_score);
      const entryZ = Number(values.entry_z_score);
      const maxZ = Number(values.max_entry_z_score);
      if (!Number.isFinite(exitZ) || !Number.isFinite(entryZ) || !Number.isFinite(maxZ) || !(exitZ < entryZ && entryZ < maxZ)) {
        errs.push(`${label}: 必须满足 出场 Z < 入场 Z < 最大入场 Z`);
      }
      const minSl = Number(values.min_stop_loss);
      const maxSl = Number(values.max_stop_loss);
      if (!Number.isFinite(minSl) || !Number.isFinite(maxSl) || minSl > maxSl) {
        errs.push(`${label}: 止损下限不能大于止损上限`);
      }
      const trigMin = Number(values.trail_trig_min);
      const trigMax = Number(values.trail_trig_max);
      if (Number.isFinite(trigMin) && Number.isFinite(trigMax) && trigMin > trigMax) {
        errs.push(`${label}: 追踪激活下限不能大于上限`);
      }
      const pbMin = Number(values.trail_pb_min);
      const pbMax = Number(values.trail_pb_max);
      if (Number.isFinite(pbMin) && Number.isFinite(pbMax) && pbMin > pbMax) {
        errs.push(`${label}: 追踪回撤下限不能大于上限`);
      }
      const cd = Number(values.cooldown_time);
      const slCd = Number(values.stop_loss_cooldown_time);
      if (!Number.isFinite(cd) || !Number.isFinite(slCd) || slCd < cd) {
        errs.push(`${label}: 止损冷静期不能短于常规冷静期`);
      }
      const ref = Number(values.executor_refresh_time);
      const tl = Number(values.time_limit);
      if (!Number.isFinite(ref) || !Number.isFinite(tl) || ref >= tl) {
        errs.push(`${label}: 挂单刷新秒必须短于持仓时限秒`);
      }
    };

    checkScope("全局参数", draft.shared);
    for (const p of enabledPairs) {
      const lev = Number(p.leverage);
      if (!Number.isInteger(lev) || lev < 1 || lev > 20) {
        errs.push(`${p.trading_pair}: 杠杆必须是 1–20 的整数`);
      }
      if (!(Number(p.total_amount_quote) > 0)) {
        errs.push(`${p.trading_pair}: 开单金额必须大于 0`);
      }
      if (Object.keys(p.overrides).length > 0) {
        checkScope(`${p.trading_pair} 独立覆盖`, { ...draft.shared, ...p.overrides });
      }
    }
    return errs;
  }, [draft]);

  if (!config || !draft) {
    return (
      <div className="cockpit-panel p-8 text-center text-[#8B96A8] text-[12.5px]">
        正在读取策略控制器配置...
      </div>
    );
  }

  const updateShared = (key: string, val: string) => {
    setDraft((prev) => (prev ? { ...prev, shared: { ...prev.shared, [key]: val } } : prev));
  };

  const updatePairField = (
    file: string,
    field: "leverage" | "total_amount_quote",
    val: string
  ) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            pairs: prev.pairs.map((p) => (p.file === file ? { ...p, [field]: val } : p)),
          }
        : prev
    );
  };

  const togglePairEnabled = (file: string) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            pairs: prev.pairs.map((p) => (p.file === file ? { ...p, enabled: !p.enabled } : p)),
          }
        : prev
    );
  };

  const setPairOverride = (file: string, key: string, val: string | null) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            pairs: prev.pairs.map((p) => {
              if (p.file !== file) return p;
              const nextOv = { ...p.overrides };
              if (val === null) {
                delete nextOv[key];
              } else {
                nextOv[key] = val;
              }
              return { ...p, overrides: nextOv };
            }),
          }
        : prev
    );
  };

  const applyPreset = (preset: StrategyPreset) => {
    setDraft((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        shared: { ...prev.shared, ...preset.shared },
      };
    });
    onToast("info", `已载入预设「${preset.name}」到草稿，请检查差异后点击保存。`);
  };

  const applyBatchToEnabled = (
    field: "leverage" | "total_amount_quote",
    value: string
  ) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            pairs: prev.pairs.map((p) => (p.enabled ? { ...p, [field]: value } : p)),
          }
        : prev
    );
  };

  const handleSave = async (andRestart: boolean) => {
    if (validationErrors.length > 0) {
      onToast("err", validationErrors[0]);
      return;
    }
    setSaving(true);
    try {
      const enabledPairs = draft.pairs.filter((p) => p.enabled);
      const res = await postSaveConfig(
        {
          enabled_files: enabledPairs.map((p) => p.file),
          pairs: enabledPairs.map((p) => ({
            file: p.file,
            leverage: p.leverage,
            total_amount_quote: p.total_amount_quote,
            overrides: p.overrides,
          })),
          shared: draft.shared,
        },
        andRestart
      );
      onToast("ok", res.message);
      await onReloadConfig();
      if (andRestart) onBotRestarted();
    } catch (err) {
      onToast("err", err instanceof Error ? err.message : "保存配置失败");
    } finally {
      setSaving(false);
    }
  };

  const handleAddPair = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddingPair(true);
    try {
      const res = await postAddPair({
        trading_pair: newPairName,
        leverage: newPairLev,
        total_amount_quote: newPairNotional,
        enable: newPairEnable,
      });
      onToast("ok", res.message);
      setAddPairOpen(false);
      setNewPairName("");
      await onReloadConfig();
    } catch (err) {
      onToast("err", err instanceof Error ? err.message : "添加交易对失败");
    } finally {
      setAddingPair(false);
    }
  };

  const activeOverridePair = overrideFile ? draft.pairs.find((p) => p.file === overrideFile) || null : null;
  const queryLower = paramQuery.trim().toLowerCase();
  const enabledCount = draft.pairs.filter((p) => p.enabled).length;

  return (
    <div className="space-y-4 pb-20">
      {/* Top Strategy Overview + Presets */}
      <div className="cockpit-panel p-4">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
          <div>
            <div className="flex items-center gap-2">
              <Sliders className="w-4 h-4 text-[#F59E0B]" />
              <h2 className="text-[13.5px] font-semibold text-[#E5E7EB]">
                STRATEGY STUDIO · 全量策略参数与交易对管理
              </h2>
              <span className="px-2 py-0.5 text-[10.5px] font-mono rounded-[3px] bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/35">
                {config.strategy.badge}
              </span>
            </div>
            <p className="text-[12px] text-[#8B96A8] mt-1">{config.strategy.description}</p>
          </div>
          <div className="flex items-center gap-2 text-[11px] font-mono">
            <span className="px-2 py-1 rounded-[3px] bg-[#0B0E14] border border-[#262F3F] text-[#CBD5E1]">
              启用中 {enabledCount} / {draft.pairs.length} 对
            </span>
          </div>
        </div>

        {/* Presets Bar */}
        <div className="pt-3 border-t border-[#1E2634]">
          <div className="flex items-center gap-1.5 text-[11px] text-[#8B96A8] mb-2">
            <Sparkles className="w-3.5 h-3.5 text-[#F59E0B]" />
            <span>快速策略预设模板（载入草稿对比差异，保存后生效）</span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
            {config.presets.map((preset) => (
              <button
                key={preset.key}
                type="button"
                onClick={() => applyPreset(preset)}
                className="text-left p-2.5 rounded-[4px] border border-[#262F3F] bg-[#0E121A] hover:border-[#F59E0B]/60 hover:bg-[#19202D] transition-colors cursor-pointer"
              >
                <div className="flex items-center justify-between gap-1 mb-1">
                  <span className="text-[12px] font-semibold text-[#E5E7EB] truncate">{preset.name}</span>
                  <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-[#F59E0B]/10 text-[#F59E0B] shrink-0">
                    {preset.badge}
                  </span>
                </div>
                <p className="text-[10.5px] text-[#8B96A8] line-clamp-2">{preset.description}</p>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Two-Column Workspace (full-width stacked below 2xl so all 7 pair columns fit without horizontal scroll) */}
      <div className="grid grid-cols-1 2xl:grid-cols-12 gap-4 items-start">
        {/* Left Column (6 cols on 2xl): Pair Matrix + Batch Tools */}
        <div className="2xl:col-span-6 cockpit-panel overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-[#1E2634] bg-[#0E121A]">
            <div>
              <h3 className="text-[13px] font-semibold text-[#E5E7EB]">
                TRADING PAIRS &amp; ALLOCATION · 交易对启停与单币种风控
              </h3>
              <p className="text-[11px] text-[#8B96A8]">
                支持启用/停用交易对、独立杠杆 (1–5x)、名义上限与单币种全参数独立覆盖
              </p>
            </div>
            <button
              type="button"
              onClick={() => setAddPairOpen(true)}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11.5px] font-semibold rounded-[4px] bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/40 hover:bg-[#F59E0B] hover:text-[#0B0E14] transition-colors cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>新增交易对</span>
            </button>
          </div>

          {/* Batch Apply Strip */}
          <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-[#1E2634] bg-[#0B0E14]/70 text-[11px]">
            <span className="text-[#8B96A8] font-medium">批量填充已启用币种:</span>
            <div className="inline-flex items-center gap-1">
              <span className="text-[#8B96A8]">杠杆</span>
              <input
                value={batchLev}
                onChange={(e) => setBatchLev(e.target.value)}
                className="cockpit-input w-11 px-1.5 py-0.5 text-center font-mono text-[11px]"
              />
              <button
                type="button"
                onClick={() => applyBatchToEnabled("leverage", batchLev)}
                className="px-1.5 py-0.5 rounded bg-[#1E2634] text-[#CBD5E1] hover:text-[#F59E0B] cursor-pointer"
              >
                应用
              </button>
            </div>
            <div className="inline-flex items-center gap-1">
              <span className="text-[#8B96A8]">金额</span>
              <input
                value={batchAmount}
                onChange={(e) => setBatchAmount(e.target.value)}
                className="cockpit-input w-14 px-1.5 py-0.5 text-center font-mono text-[11px]"
              />
              <button
                type="button"
                onClick={() => applyBatchToEnabled("total_amount_quote", batchAmount)}
                className="px-1.5 py-0.5 rounded bg-[#1E2634] text-[#CBD5E1] hover:text-[#F59E0B] cursor-pointer"
              >
                应用
              </button>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-[11.5px]">
              <thead>
                <tr className="border-b border-[#1E2634] bg-[#0E121A] text-[10px] font-mono uppercase text-[#8B96A8]">
                  <th className="py-2 px-3">启用</th>
                  <th className="py-2 px-2">Pair</th>
                  <th className="py-2 px-2">杠杆</th>
                  <th className="py-2 px-2">名义上限</th>
                  <th className="py-2 px-3 text-right">独立覆盖</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1E2634]/70">
                {draft.pairs.map((p) => {
                  const ovCount = Object.keys(p.overrides).length;
                  const effMode = p.overrides.signal_mode ?? draft.shared.signal_mode;
                  const symbol = p.trading_pair.split("-")[0];
                  return (
                    <tr
                      key={p.file}
                      className={`transition-colors ${
                        p.enabled ? "hover:bg-[#19202D]/50" : "opacity-50 bg-[#0B0E14]/50"
                      }`}
                    >
                      <td className="py-2 px-3">
                        <button
                          type="button"
                          onClick={() => togglePairEnabled(p.file)}
                          className={`w-8 h-4.5 rounded-full relative transition-colors cursor-pointer ${
                            p.enabled ? "bg-[#10B981]" : "bg-[#262F3F]"
                          }`}
                          aria-label={`切换 ${p.trading_pair}`}
                        >
                          <span
                            className={`block w-3.5 h-3.5 rounded-full bg-white transition-transform absolute top-0.5 ${
                              p.enabled ? "translate-x-4" : "translate-x-0.5"
                            }`}
                          />
                        </button>
                      </td>
                      <td className="py-2 px-2">
                        <CoinBadge symbol={symbol} pair={p.trading_pair} compact />
                        <div className="flex items-center gap-1 mt-0.5 pl-7 font-mono text-[9.5px]">
                          <span
                            className={
                              effMode === "momentum" ? "text-[#F59E0B]" : "text-[#38BDF8]"
                            }
                          >
                            {effMode.toUpperCase()}
                          </span>
                          {ovCount > 0 && (
                            <span className="px-1 rounded bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/30">
                              OVERRIDE {ovCount}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2 px-2">
                        <input
                          value={p.leverage}
                          onChange={(e) => updatePairField(p.file, "leverage", e.target.value)}
                          className="cockpit-input w-12 px-1.5 py-1 text-right font-mono"
                          inputMode="numeric"
                        />
                      </td>
                      <td className="py-2 px-2">
                        <input
                          value={p.total_amount_quote}
                          onChange={(e) => updatePairField(p.file, "total_amount_quote", e.target.value)}
                          className="cockpit-input w-16 px-1.5 py-1 text-right font-mono"
                          inputMode="decimal"
                        />
                      </td>
                      <td className="py-2 px-3 text-right">
                        <button
                          type="button"
                          onClick={() => setOverrideFile(p.file)}
                          className={`px-2 py-1 text-[10.5px] rounded-[3px] border cursor-pointer ${
                            ovCount > 0
                              ? "border-[#F59E0B]/60 bg-[#F59E0B]/15 text-[#F59E0B]"
                              : "border-[#262F3F] bg-[#0B0E14] text-[#8B96A8] hover:text-[#E5E7EB]"
                          }`}
                        >
                          {ovCount > 0 ? `覆盖(${ovCount})` : "覆盖"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Right Column (6 cols on 2xl): Searchable Collapsible 5-Group Parameter Editor */}
        <div className="2xl:col-span-6 space-y-3">
          <div className="cockpit-panel px-3.5 py-2.5 flex flex-wrap items-center justify-between gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="w-3.5 h-3.5 text-[#8B96A8] absolute left-2.5 top-1/2 -translate-y-1/2" />
              <input
                value={paramQuery}
                onChange={(e) => setParamQuery(e.target.value)}
                placeholder="搜索策略参数（中/英/字段名，例如 atr、momentum、z_score）..."
                className="cockpit-input w-full pl-8 pr-3 py-1.5 text-[12px]"
              />
            </div>
            <div className="flex items-center gap-1.5 text-[11px]">
              <button
                type="button"
                onClick={() =>
                  setOpenGroups({ mode: true, human: true, momentum: true, reversion: true, risk: true, execution: true })
                }
                className="px-2 py-1 rounded border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
              >
                全部展开
              </button>
              <button
                type="button"
                onClick={() =>
                  setOpenGroups({ mode: false, human: false, momentum: false, reversion: false, risk: false, execution: false })
                }
                className="px-2 py-1 rounded border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
              >
                全部折叠
              </button>
            </div>
          </div>

          {GROUPS.map((group) => {
            const fields = sharedSpecs.filter((f) => {
              if (f.group !== group.id) return false;
              if (!queryLower) return true;
              return (
                f.key.toLowerCase().includes(queryLower) ||
                f.label.toLowerCase().includes(queryLower) ||
                f.label_en.toLowerCase().includes(queryLower) ||
                f.description.toLowerCase().includes(queryLower)
              );
            });
            if (fields.length === 0) return null;
            const isOpen = queryLower ? true : Boolean(openGroups[group.id]);

            return (
              <div key={group.id} className="cockpit-panel overflow-hidden">
                <button
                  type="button"
                  onClick={() => setOpenGroups((prev) => ({ ...prev, [group.id]: !prev[group.id] }))}
                  className="w-full flex items-center justify-between px-4 py-2.5 bg-[#0E121A] hover:bg-[#19202D] text-left cursor-pointer"
                >
                  <div>
                    <div className="text-[12.5px] font-semibold text-[#E5E7EB]">{group.title}</div>
                    <div className="text-[10.5px] text-[#8B96A8]">{group.subtitle}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10.5px] font-mono text-[#8B96A8]">{fields.length} 项</span>
                    {isOpen ? (
                      <ChevronDown className="w-4 h-4 text-[#F59E0B]" />
                    ) : (
                      <ChevronRight className="w-4 h-4 text-[#8B96A8]" />
                    )}
                  </div>
                </button>

                {isOpen && (
                  <div className="p-3.5 grid grid-cols-1 sm:grid-cols-2 gap-3 border-t border-[#1E2634]">
                    {fields.map((spec) => {
                      const val = draft.shared[spec.key] ?? "";
                      const isMixed = Boolean(config.shared_mixed[spec.key]);
                      return (
                        <div
                          key={spec.key}
                          className="p-2.5 rounded-[4px] border border-[#1E2634] bg-[#0B0E14]/70 flex flex-col justify-between gap-2"
                        >
                          <div>
                            <div className="flex items-center justify-between gap-1">
                              <span className="text-[12px] font-medium text-[#E5E7EB]">
                                {spec.label}{" "}
                                <span className="text-[10.5px] text-[#8B96A8] font-normal">
                                  {spec.label_en}
                                </span>
                              </span>
                              {isMixed && (
                                <span
                                  className="px-1.5 py-0.5 text-[9.5px] font-mono rounded bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/30"
                                  title="当前各币种此参数不一致，保存全局修改会统一或保留独立覆盖"
                                >
                                  MIXED
                                </span>
                              )}
                            </div>
                            <p className="text-[10.5px] text-[#8B96A8] mt-0.5 leading-snug">
                              {spec.description}
                            </p>
                          </div>

                          <div className="flex items-center gap-2">
                            {spec.kind === "choice" ? (
                              <select
                                value={val}
                                onChange={(e) => updateShared(spec.key, e.target.value)}
                                className="cockpit-input w-full px-2.5 py-1.5 text-[12px] font-mono"
                              >
                                {(spec.choices || []).map((opt) => (
                                  <option key={opt} value={opt}>
                                    {opt}
                                  </option>
                                ))}
                              </select>
                            ) : spec.kind === "bool" ? (
                              <button
                                type="button"
                                onClick={() => updateShared(spec.key, val === "true" ? "false" : "true")}
                                className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-[4px] border font-mono text-[12px] cursor-pointer ${
                                  val === "true"
                                    ? "border-[#10B981]/40 bg-[#10B981]/10 text-[#10B981]"
                                    : "border-[#262F3F] bg-[#0E121A] text-[#8B96A8]"
                                }`}
                              >
                                <span>{val === "true" ? "TRUE · 已开启" : "FALSE · 已关闭"}</span>
                                <span
                                  className={`w-7 h-4 rounded-full relative ${
                                    val === "true" ? "bg-[#10B981]" : "bg-[#262F3F]"
                                  }`}
                                >
                                  <span
                                    className={`block w-3 h-3 rounded-full bg-white absolute top-0.5 transition-transform ${
                                      val === "true" ? "translate-x-3.5" : "translate-x-0.5"
                                    }`}
                                  />
                                </span>
                              </button>
                            ) : (
                              <div className="flex items-center gap-1.5 w-full">
                                <input
                                  value={val}
                                  onChange={(e) => updateShared(spec.key, e.target.value)}
                                  inputMode="decimal"
                                  className="cockpit-input flex-1 px-2.5 py-1.5 text-[12px] font-mono text-right"
                                />
                                {spec.unit && (
                                  <span className="px-2 py-1.5 text-[10.5px] font-mono rounded-[4px] bg-[#141923] border border-[#262F3F] text-[#8B96A8] shrink-0">
                                    {spec.unit}
                                  </span>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Validation Errors Banner */}
      {validationErrors.length > 0 && (
        <div className="cockpit-panel border-[#EF4444]/50 bg-[#EF4444]/10 px-4 py-3 text-[12px] text-[#FCA5A5]">
          <div className="flex items-center gap-2 font-semibold text-[#EF4444] mb-1">
            <AlertTriangle className="w-4 h-4" />
            <span>参数约束校验未通过（请修正后再保存）</span>
          </div>
          <ul className="list-disc pl-5 space-y-0.5 font-mono text-[11px]">
            {validationErrors.map((e, idx) => (
              <li key={idx}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Sticky Bottom Action & Diff Bar */}
      <div className="sticky bottom-3 z-20 cockpit-panel bg-[#0E121A]/95 backdrop-blur-md border-[#F59E0B]/40 px-4 py-3 shadow-2xl flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span
            className={`px-2 py-1 text-[11px] font-mono rounded-[3px] border ${
              diffs.length > 0
                ? "bg-[#F59E0B]/15 text-[#F59E0B] border-[#F59E0B]/40"
                : "bg-[#141923] text-[#8B96A8] border-[#262F3F]"
            }`}
          >
            {diffs.length > 0 ? `${diffs.length} 项待保存变更` : "暂无未保存修改"}
          </span>
          {diffs.length > 0 && (
            <button
              type="button"
              onClick={() => setDiffOpen(true)}
              className="inline-flex items-center gap-1 text-[11.5px] text-[#38BDF8] hover:underline cursor-pointer"
            >
              <Eye className="w-3.5 h-3.5" />
              <span>查看变更对比 Diff</span>
            </button>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={diffs.length === 0 || saving}
            onClick={() => config && setDraft(buildDraft(config))}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] rounded-[4px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] disabled:opacity-40 cursor-pointer"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>重置草稿</span>
          </button>
          <button
            type="button"
            disabled={saving || validationErrors.length > 0}
            onClick={() => handleSave(false)}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 text-[12px] font-medium rounded-[4px] border border-[#F59E0B]/50 bg-[#F59E0B]/10 text-[#F59E0B] hover:bg-[#F59E0B]/20 disabled:opacity-40 cursor-pointer"
          >
            <Save className="w-3.5 h-3.5" />
            <span>仅保存配置</span>
          </button>
          <button
            type="button"
            disabled={saving || validationErrors.length > 0}
            onClick={() => handleSave(true)}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 text-[12px] font-semibold rounded-[4px] bg-[#F59E0B] hover:bg-[#D97706] text-[#0B0E14] disabled:opacity-40 cursor-pointer"
          >
            <Zap className="w-3.5 h-3.5" />
            <span>{saving ? "保存并重启中..." : "保存并重启生效 Save & Restart"}</span>
          </button>
        </div>
      </div>

      {/* Per-Pair Independent Override Modal */}
      <Modal
        open={Boolean(activeOverridePair)}
        title={`单币种独立参数覆盖 · ${activeOverridePair?.trading_pair || ""}`}
        onClose={() => setOverrideFile(null)}
        maxWidthClass="max-w-3xl"
      >
        {activeOverridePair && (
          <div className="space-y-3">
            <p className="text-[11.5px] text-[#8B96A8]">
              未勾选“独立覆盖”的参数将自动继承右侧全局设定；勾选后可单独为{" "}
              <span className="text-[#F59E0B] font-mono">{activeOverridePair.trading_pair}</span>{" "}
              定制信号模式（如动量/回归混跑）、K 线周期、Z 阈值或 ATR 止损倍数。
            </p>
            <div className="max-h-[58vh] overflow-y-auto grid grid-cols-1 sm:grid-cols-2 gap-2.5 pr-1">
              {sharedSpecs.map((spec) => {
                const isOverridden = Object.prototype.hasOwnProperty.call(
                  activeOverridePair.overrides,
                  spec.key
                );
                const effVal = isOverridden
                  ? activeOverridePair.overrides[spec.key]
                  : draft.shared[spec.key];
                return (
                  <div
                    key={spec.key}
                    className={`p-2.5 rounded-[4px] border ${
                      isOverridden
                        ? "border-[#F59E0B]/60 bg-[#F59E0B]/5"
                        : "border-[#1E2634] bg-[#0B0E14]"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1.5">
                      <span className="text-[11.5px] font-medium text-[#E5E7EB]">
                        {spec.label} <span className="text-[#8B96A8] text-[10px]">{spec.key}</span>
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          setPairOverride(
                            activeOverridePair.file,
                            spec.key,
                            isOverridden ? null : draft.shared[spec.key]
                          )
                        }
                        className={`px-1.5 py-0.5 text-[10px] font-mono rounded border cursor-pointer ${
                          isOverridden
                            ? "bg-[#F59E0B] text-[#0B0E14] border-[#F59E0B] font-semibold"
                            : "border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB]"
                        }`}
                      >
                        {isOverridden ? "独立覆盖中" : "跟随全局"}
                      </button>
                    </div>
                    {spec.kind === "choice" ? (
                      <select
                        disabled={!isOverridden}
                        value={effVal}
                        onChange={(e) => setPairOverride(activeOverridePair.file, spec.key, e.target.value)}
                        className="cockpit-input w-full px-2 py-1 text-[11.5px] font-mono disabled:opacity-60"
                      >
                        {(spec.choices || []).map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    ) : spec.kind === "bool" ? (
                      <button
                        type="button"
                        disabled={!isOverridden}
                        onClick={() =>
                          setPairOverride(
                            activeOverridePair.file,
                            spec.key,
                            effVal === "true" ? "false" : "true"
                          )
                        }
                        className="cockpit-input w-full px-2 py-1 text-left text-[11.5px] font-mono disabled:opacity-60 cursor-pointer"
                      >
                        {effVal}
                      </button>
                    ) : (
                      <input
                        disabled={!isOverridden}
                        value={effVal}
                        onChange={(e) => setPairOverride(activeOverridePair.file, spec.key, e.target.value)}
                        className="cockpit-input w-full px-2 py-1 text-[11.5px] font-mono text-right disabled:opacity-60"
                      />
                    )}
                  </div>
                );
              })}
            </div>
            <div className="flex justify-end pt-2 border-t border-[#1E2634]">
              <button
                type="button"
                onClick={() => setOverrideFile(null)}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-[12px] font-semibold rounded-[4px] bg-[#F59E0B] text-[#0B0E14] cursor-pointer"
              >
                <Check className="w-3.5 h-3.5" />
                <span>完成返回</span>
              </button>
            </div>
          </div>
        )}
      </Modal>

      {/* Diff Preview Modal */}
      <Modal
        open={diffOpen}
        title={`待保存参数变更对比 Diff (${diffs.length})`}
        onClose={() => setDiffOpen(false)}
        maxWidthClass="max-w-2xl"
      >
        <div className="max-h-[55vh] overflow-y-auto divide-y divide-[#1E2634] text-[11.5px] font-mono">
          {diffs.map((d, idx) => (
            <div key={idx} className="py-2 flex items-center justify-between gap-2">
              <div>
                <span className="text-[#F59E0B]">{d.target}</span>
                <span className="text-[#8B96A8] mx-1.5">·</span>
                <span className="text-[#CBD5E1]">{d.field}</span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <span className="px-1.5 py-0.5 rounded bg-[#EF4444]/10 text-[#EF4444] line-through">
                  {d.from}
                </span>
                <span className="text-[#8B96A8]">→</span>
                <span className="px-1.5 py-0.5 rounded bg-[#10B981]/15 text-[#10B981]">
                  {d.to}
                </span>
              </div>
            </div>
          ))}
        </div>
      </Modal>

      {/* Add New Trading Pair Modal */}
      <Modal open={addPairOpen} title="新增 OKX 永续交易对控制器" onClose={() => setAddPairOpen(false)}>
        <form onSubmit={handleAddPair} className="space-y-3 text-[12px]">
          <div>
            <label className="block text-[#8B96A8] mb-1">交易对符号 Trading Pair (如 LINK-USDT)</label>
            <input
              required
              value={newPairName}
              onChange={(e) => setNewPairName(e.target.value.toUpperCase())}
              placeholder="LINK-USDT"
              className="cockpit-input w-full px-3 py-1.5 font-mono"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[#8B96A8] mb-1">杠杆 Leverage (1–20)</label>
              <input
                value={newPairLev}
                onChange={(e) => setNewPairLev(e.target.value)}
                className="cockpit-input w-full px-3 py-1.5 font-mono"
              />
            </div>
            <div>
              <label className="block text-[#8B96A8] mb-1">名义仓位上限 USDT</label>
              <input
                value={newPairNotional}
                onChange={(e) => setNewPairNotional(e.target.value)}
                className="cockpit-input w-full px-3 py-1.5 font-mono"
              />
            </div>
          </div>
          <label className="flex items-center gap-2 text-[#CBD5E1] cursor-pointer pt-1">
            <input
              type="checkbox"
              checked={newPairEnable}
              onChange={(e) => setNewPairEnable(e.target.checked)}
              className="accent-[#F59E0B]"
            />
            <span>立即加入 conf_okx_multi.yml 并启用（默认双向 long_only: false）</span>
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setAddPairOpen(false)}
              className="px-3 py-1.5 rounded-[4px] border border-[#262F3F] text-[#8B96A8] cursor-pointer"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={addingPair}
              className="px-3.5 py-1.5 font-semibold rounded-[4px] bg-[#F59E0B] text-[#0B0E14] disabled:opacity-50 cursor-pointer"
            >
              {addingPair ? "创建中..." : "创建控制器"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
};
