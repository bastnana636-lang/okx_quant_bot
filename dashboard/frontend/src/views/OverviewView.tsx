import React, { useMemo, useState } from "react";
import { Activity, BarChart3, Clock, DollarSign, Search, TrendingUp, CandlestickChart } from "lucide-react";
import type { DashboardView, PerfRow } from "../types";
import { fmtSigned, fmtUsd, pnlColorClass } from "../utils/format";
import { CoinBadge } from "../components/CoinBadge";
import { EquityChart } from "../components/EquityChart";
import { ConfirmActionModal } from "../components/ConfirmModal";

interface OverviewViewProps {
  view: DashboardView;
  onOpenMarket: (pair: string) => void;
  onCloseController: (controller: string) => Promise<void>;
}

export const OverviewView: React.FC<OverviewViewProps> = ({
  view,
  onOpenMarket,
  onCloseController,
}) => {
  const [filterText, setFilterText] = useState("");
  const [modeFilter, setModeFilter] = useState<"all" | "human_swing" | "momentum" | "reversion">("all");
  const [closingTarget, setClosingTarget] = useState<PerfRow | null>(null);
  const [closeBusy, setCloseBusy] = useState(false);

  const filteredRows = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    return view.perf_rows.filter((r) => {
      if (modeFilter !== "all" && r.signal_mode !== modeFilter) return false;
      if (!q) return true;
      return (
        r.pair.toLowerCase().includes(q) ||
        r.controller.toLowerCase().includes(q) ||
        r.reason.toLowerCase().includes(q)
      );
    });
  }, [view.perf_rows, filterText, modeFilter]);

  const handleConfirmClose = async () => {
    if (!closingTarget) return;
    setCloseBusy(true);
    try {
      await onCloseController(closingTarget.controller);
    } finally {
      setCloseBusy(false);
      setClosingTarget(null);
    }
  };

  return (
    <div className="space-y-4">
      {/* 4-Card KPI Ribbon */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <div className="cockpit-panel p-3.5 border-l-2 border-l-[#F59E0B]">
          <div className="flex items-center justify-between text-[11px] text-[#8B96A8] mb-1.5">
            <span>TOTAL ASSETS 总资产</span>
            <DollarSign className="w-3.5 h-3.5 text-[#F59E0B]" />
          </div>
          <div className="text-[22px] font-mono font-semibold text-[#E5E7EB] leading-tight">
            {fmtUsd(view.usdt_bal, false, 2)}{" "}
            <span className="text-[11px] text-[#F59E0B] font-normal">USDT</span>
          </div>
          <div className="text-[11px] text-[#8B96A8] mt-1">
            OKX 永续账户实时保证金权益
          </div>
        </div>

        <div className="cockpit-panel p-3.5">
          <div className="flex items-center justify-between text-[11px] text-[#8B96A8] mb-1.5">
            <span>GLOBAL PNL 总盈亏</span>
            <TrendingUp className="w-3.5 h-3.5 text-[#10B981]" />
          </div>
          <div className={`text-[22px] font-mono font-semibold leading-tight ${pnlColorClass(view.global_pnl)}`}>
            {fmtSigned(view.global_pnl, 4)}{" "}
            <span className="text-[12px] font-medium opacity-90">({view.global_pct})</span>
          </div>
          <div className="text-[11px] text-[#8B96A8] mt-1">
            已实现与未实现实时求和 · 累计成交 {fmtUsd(view.total_volume, false, 0)}
          </div>
        </div>

        <div className="cockpit-panel p-3.5">
          <div className="flex items-center justify-between text-[11px] text-[#8B96A8] mb-1.5">
            <span>REALIZED / UNREALIZED</span>
            <span className="px-1.5 py-0.5 text-[10px] font-mono rounded-[3px] bg-[#10B981]/10 text-[#10B981] border border-[#10B981]/30">
              重启清零
            </span>
          </div>
          <div className="text-[18px] font-mono font-semibold leading-tight pt-0.5">
            <span className={pnlColorClass(view.realized)}>{fmtSigned(view.realized, 4)}</span>
            <span className="text-[#4B5563] mx-1.5">/</span>
            <span className={pnlColorClass(view.unrealized)}>{fmtSigned(view.unrealized, 4)}</span>
          </div>
          <div className="text-[11px] text-[#8B96A8] mt-1.5">
            已落袋盈亏 / 当前浮动盈亏 (USDT)
          </div>
        </div>

        <div className="cockpit-panel p-3.5">
          <div className="flex items-center justify-between text-[11px] text-[#8B96A8] mb-1.5">
            <span>ENGINE &amp; STRATEGY 引擎策略</span>
            <Clock className="w-3.5 h-3.5 text-[#38BDF8]" />
          </div>
          <div className="text-[20px] font-mono font-semibold text-[#E5E7EB] leading-tight">
            {view.uptime_str}
          </div>
          <div className="flex items-center gap-2 mt-1.5 text-[11px]">
            <span className="px-1.5 py-0.5 rounded-[3px] bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/30 font-mono">
              {view.strategy_info?.badge || "1h 策略"}
            </span>
            <span className="text-[#8B96A8] truncate">{view.perf_rows.length} 交易对监控中</span>
          </div>
        </div>
      </div>

      {/* Dual-Track Trajectory Chart */}
      <EquityChart
        history={view.history}
        botSessionPnl={view.global_pnl}
        sessionPeak={view.session_peak}
        sessionMin={view.session_min}
        usdtBal={view.usdt_bal}
      />

      {/* Controller Signal & Performance Matrix */}
      <div className="cockpit-panel">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 border-b border-[#1E2634]">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-4 h-4 text-[#F59E0B]" />
            <div>
              <h2 className="text-[13px] font-semibold tracking-wide text-[#E5E7EB]">
                CONTROLLER SIGNAL &amp; PERFORMANCE MATRIX · 策略信号与绩效矩阵
              </h2>
              <p className="text-[11px] text-[#8B96A8]">
                实时展示各控制器信号强度、Z-Score / 动量读数、杠杆配额与盈亏归因
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex bg-[#0B0E14] border border-[#262F3F] rounded-[4px] p-0.5 text-[11px]">
              {(
                [
                  { id: "all", label: "全部" },
                  { id: "human_swing", label: "HUMAN 波段" },
                  { id: "momentum", label: "动量 MOMENTUM" },
                  { id: "reversion", label: "回归 REVERSION" },
                ] as const
              ).map((m) => (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => setModeFilter(m.id)}
                  className={`px-2 py-1 rounded-[3px] cursor-pointer ${
                    modeFilter === m.id
                      ? "bg-[#F59E0B]/15 text-[#F59E0B]"
                      : "text-[#8B96A8] hover:text-[#E5E7EB]"
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
            <div className="relative">
              <Search className="w-3.5 h-3.5 text-[#8B96A8] absolute left-2.5 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
                placeholder="筛选币种或状态..."
                className="cockpit-input pl-7 pr-2.5 py-1 text-[11.5px] w-44"
              />
            </div>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-[#1E2634] bg-[#0E121A] text-[10.5px] font-mono uppercase tracking-wider text-[#8B96A8]">
                <th className="py-2.5 px-4">Pair 交易对</th>
                <th className="py-2.5 px-3">Mode / 杠杆</th>
                <th className="py-2.5 px-3">Signal / 指标读数</th>
                <th className="py-2.5 px-3 text-right">Realized 已实现</th>
                <th className="py-2.5 px-3 text-right">Unrealized 未实现</th>
                <th className="py-2.5 px-3 text-right">Global 净盈亏</th>
                <th className="py-2.5 px-3 text-right">Volume 成交额</th>
                <th className="py-2.5 px-4 text-right">Action 操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E2634]/70">
              {filteredRows.length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-8 text-center text-[#8B96A8] text-[12px]">
                    暂无匹配的控制器行
                  </td>
                </tr>
              ) : (
                filteredRows.map((row) => {
                  const isHuman = row.signal_mode === "human_swing";
                  const isMom = row.signal_mode === "momentum";
                  const metricVal = isHuman
                    ? (row.rsi9 ?? row.z_score)
                    : isMom
                    ? (row.momentum ?? row.z_score)
                    : row.z_score;
                  const threshold = isHuman ? 100 : isMom ? row.momentum_threshold || 0.008 : row.entry_z_score || 2.0;
                  const ratio =
                    metricVal !== null && threshold > 0
                      ? Math.min(1, isHuman ? metricVal / 100 : Math.abs(metricVal) / (threshold * 1.5))
                      : 0;
                  const barPos = isHuman ? (metricVal ?? 50) >= 50 : (metricVal ?? 0) >= 0;

                  return (
                    <tr key={row.controller} className="hover:bg-[#19202D]/60 transition-colors">
                      <td className="py-2.5 px-4">
                        <div className="flex items-center gap-2">
                          <CoinBadge symbol={row.symbol} pair={row.pair} compact />
                          {row.manual_kill_switch && (
                            <span className="px-1.5 py-0.5 text-[9.5px] font-mono rounded bg-[#EF4444]/15 text-[#EF4444] border border-[#EF4444]/30">
                              PAUSED
                            </span>
                          )}
                        </div>
                      </td>

                      <td className="py-2.5 px-3 whitespace-nowrap">
                        <div className="flex items-center gap-1.5 font-mono text-[10.5px]">
                          <span
                            className={`px-1.5 py-0.5 rounded-[3px] border whitespace-nowrap ${
                              isHuman
                                ? "bg-[#10B981]/10 text-[#10B981] border-[#10B981]/35"
                                : isMom
                                ? "bg-[#F59E0B]/10 text-[#F59E0B] border-[#F59E0B]/35"
                                : "bg-[#38BDF8]/10 text-[#38BDF8] border-[#38BDF8]/35"
                            }`}
                          >
                            {isHuman ? "HUMAN" : isMom ? "MOMENTUM" : "REVERSION"} · {row.candles_interval}
                          </span>
                          <span className="px-1.5 py-0.5 rounded-[3px] bg-[#1E2634] text-[#F59E0B] whitespace-nowrap">
                            {row.leverage}x
                          </span>
                        </div>
                      </td>

                      <td className="py-2.5 px-3 min-w-[200px]">
                        <div className="flex items-center gap-2">
                          <div className="w-24 h-2 bg-[#0B0E14] border border-[#262F3F] rounded-[2px] overflow-hidden shrink-0">
                            <div
                              className={`h-full transition-all ${
                                barPos ? "bg-[#10B981]" : "bg-[#EF4444]"
                              }`}
                              style={{ width: `${Math.round(ratio * 100)}%` }}
                            />
                          </div>
                          <span
                            className={`font-mono text-[11.5px] ${
                              row.signal === 1
                                ? "text-[#10B981] font-semibold"
                                : row.signal === -1
                                ? "text-[#EF4444] font-semibold"
                                : "text-[#CBD5E1]"
                            }`}
                            title={row.reason}
                          >
                            {metricVal === null
                              ? "n/a"
                              : isHuman
                              ? `RSI ${metricVal.toFixed(1)}${row.er4h !== null && row.er4h !== undefined ? ` · ER ${row.er4h.toFixed(2)}` : ""}`
                              : isMom
                              ? `${(metricVal * 100).toFixed(2)}%`
                              : `${metricVal >= 0 ? "+" : ""}${metricVal.toFixed(2)}σ`}
                          </span>
                          {row.signal !== 0 && (
                            <span
                              className={`px-1.5 py-0.5 text-[10px] font-mono rounded-[3px] ${
                                row.signal === 1
                                  ? "bg-[#10B981]/15 text-[#10B981] border border-[#10B981]/40"
                                  : "bg-[#EF4444]/15 text-[#EF4444] border border-[#EF4444]/40"
                              }`}
                            >
                              {row.signal === 1 ? "BUY" : "SELL"}
                            </span>
                          )}
                        </div>
                        {row.reason && (
                          <div className="text-[10px] text-[#8B96A8] truncate max-w-[240px] mt-0.5">
                            {row.reason}
                          </div>
                        )}
                      </td>

                      <td className={`py-2.5 px-3 text-right font-mono ${pnlColorClass(row.realized)}`}>
                        {fmtSigned(row.realized, 4)}
                      </td>
                      <td className={`py-2.5 px-3 text-right font-mono ${pnlColorClass(row.unrealized)}`}>
                        {fmtSigned(row.unrealized, 4)}
                      </td>
                      <td className={`py-2.5 px-3 text-right font-mono font-semibold ${pnlColorClass(row.global)}`}>
                        {fmtSigned(row.global, 4)}{" "}
                        <span className="text-[10.5px] font-normal opacity-80">({row.global_pct})</span>
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono text-[#CBD5E1]">
                        {row.volume.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </td>
                      <td className="py-2.5 px-4 text-right whitespace-nowrap">
                        <div className="inline-flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => onOpenMarket(row.pair)}
                            className="inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-[3px] border border-[#262F3F] bg-[#0B0E14] text-[#8B96A8] hover:text-[#F59E0B] hover:border-[#F59E0B]/50 whitespace-nowrap cursor-pointer"
                            title="在行情 K 线图查看"
                          >
                            <CandlestickChart className="w-3 h-3 shrink-0" />
                            <span>K线</span>
                          </button>
                          <button
                            type="button"
                            onClick={() => setClosingTarget(row)}
                            className="px-2 py-1 text-[11px] rounded-[3px] border border-[#EF4444]/40 bg-[#EF4444]/10 text-[#EF4444] hover:bg-[#EF4444] hover:text-white transition-colors whitespace-nowrap cursor-pointer"
                          >
                            平仓
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
            {filteredRows.length > 0 && (
              <tfoot>
                <tr className="border-t border-[#262F3F] bg-[#0E121A] font-mono text-[12px] font-semibold">
                  <td className="py-2.5 px-4 text-[#F59E0B] flex items-center gap-1.5">
                    <Activity className="w-3.5 h-3.5" />
                    <span>GLOBAL TOTAL 汇总</span>
                  </td>
                  <td className="py-2.5 px-3 text-[#8B96A8] text-[11px]">{view.perf_rows.length} controllers</td>
                  <td className="py-2.5 px-3 text-[#8B96A8]">-</td>
                  <td className={`py-2.5 px-3 text-right ${pnlColorClass(view.realized)}`}>
                    {fmtSigned(view.realized, 4)}
                  </td>
                  <td className={`py-2.5 px-3 text-right ${pnlColorClass(view.unrealized)}`}>
                    {fmtSigned(view.unrealized, 4)}
                  </td>
                  <td className={`py-2.5 px-3 text-right ${pnlColorClass(view.global_pnl)}`}>
                    {fmtSigned(view.global_pnl, 4)} ({view.global_pct})
                  </td>
                  <td className="py-2.5 px-3 text-right text-[#E5E7EB]">
                    {view.total_volume.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="py-2.5 px-4" />
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </div>

      <ConfirmActionModal
        open={Boolean(closingTarget)}
        title={`确认市价平仓 · ${closingTarget?.pair || ""}`}
        description={`将向运行中的控制器 [${closingTarget?.controller || ""}] 发送市价平仓指令，并在下一轮行情周期撤销未成交开仓挂单。是否继续？`}
        confirmLabel="确认平仓 Close"
        tone="red"
        busy={closeBusy}
        onConfirm={handleConfirmClose}
        onClose={() => setClosingTarget(null)}
      />
    </div>
  );
};
