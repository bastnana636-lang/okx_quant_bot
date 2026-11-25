import React, { useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Layers, ListOrdered, Trash2 } from "lucide-react";
import type { DashboardView, PositionItem } from "../types";
import { fmtPrice, fmtSigned, fmtUsd, pnlColorClass } from "../utils/format";
import { CoinBadge } from "../components/CoinBadge";
import { ConfirmActionModal, ManualCloseModal } from "../components/ConfirmModal";

interface PositionsViewProps {
  view: DashboardView;
  onCloseController: (controller: string) => Promise<void>;
  onManualClose: (payload: {
    pair: string;
    side: string;
    amount: number;
    breakeven: number;
    pnl: number;
  }) => Promise<void>;
  onDeleteManualClose: (pair: string, side: string) => Promise<void>;
}

export const PositionsView: React.FC<PositionsViewProps> = ({
  view,
  onCloseController,
  onManualClose,
  onDeleteManualClose,
}) => {
  const [execFilter, setExecFilter] = useState<"all" | "RUNNING" | "TERMINATED">("all");
  const [manualTarget, setManualTarget] = useState<PositionItem | null>(null);
  const [closingPos, setClosingPos] = useState<PositionItem | null>(null);
  const [busy, setBusy] = useState(false);

  const filteredExecutors = useMemo(() => {
    if (execFilter === "all") return view.executors;
    return view.executors.filter((e) => e.status === execFilter);
  }, [view.executors, execFilter]);

  return (
    <div className="space-y-4">
      {view.warnings.length > 0 && (
        <div className="cockpit-panel border-[#F59E0B]/40 bg-[#F59E0B]/5 px-4 py-3">
          <div className="flex items-center gap-2 text-[#F59E0B] text-[12px] font-semibold mb-1.5">
            <AlertCircle className="w-4 h-4" />
            <span>ENGINE WARNINGS · 引擎运行警告 ({view.warnings.length})</span>
          </div>
          <ul className="space-y-0.5 text-[11px] font-mono text-[#FDE68A]/90 max-h-28 overflow-y-auto">
            {view.warnings.map((w, i) => (
              <li key={i}>• {w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Active Margin Positions */}
      <div className="cockpit-panel">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-[#1E2634]">
          <div>
            <h2 className="text-[13px] font-semibold text-[#E5E7EB]">
              ACTIVE MARGIN POSITIONS · 活跃合约持仓 ({view.positions.length})
            </h2>
            {view.manual_close_note && (
              <p className="text-[11px] text-[#10B981] font-mono mt-0.5">{view.manual_close_note}</p>
            )}
          </div>
          <span className="text-[11px] text-[#8B96A8]">支持一键市价平仓或手工交易所平仓补录盈亏</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-[#1E2634] bg-[#0E121A] text-[10.5px] font-mono uppercase text-[#8B96A8]">
                <th className="py-2.5 px-4">Pair / Source</th>
                <th className="py-2.5 px-3">Side</th>
                <th className="py-2.5 px-3 text-right">Amount</th>
                <th className="py-2.5 px-3 text-right">Notional (USDT)</th>
                <th className="py-2.5 px-3 text-right">Breakeven</th>
                <th className="py-2.5 px-3 text-right">Unrealized</th>
                <th className="py-2.5 px-3 text-right">Realized</th>
                <th className="py-2.5 px-3 text-right">Fees</th>
                <th className="py-2.5 px-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E2634]/70">
              {view.positions.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-8 text-center text-[#8B96A8]">
                    当前暂无活跃持仓，策略持续监控入场机会
                  </td>
                </tr>
              ) : (
                view.positions.map((pos) => (
                  <tr key={`${pos.pair}-${pos.side}`} className="hover:bg-[#19202D]/60">
                    <td className="py-2.5 px-4">
                      <div className="flex items-center gap-2">
                        <CoinBadge symbol={pos.symbol} pair={pos.pair} compact />
                        <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-[#1E2634] text-[#8B96A8]">
                          {pos.source}
                        </span>
                      </div>
                    </td>
                    <td className="py-2.5 px-3 font-mono">
                      <span
                        className={`px-1.5 py-0.5 text-[10.5px] font-semibold rounded-[3px] ${
                          pos.side === "BUY"
                            ? "bg-[#10B981]/15 text-[#10B981]"
                            : "bg-[#EF4444]/15 text-[#EF4444]"
                        }`}
                      >
                        {pos.side === "BUY" ? "LONG" : "SHORT"}
                      </span>
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-[#E5E7EB]">{pos.amount}</td>
                    <td className="py-2.5 px-3 text-right font-mono text-[#E5E7EB]">
                      {fmtUsd(pos.value, false, 2)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-[#CBD5E1]">
                      {fmtPrice(pos.breakeven)}
                    </td>
                    <td className={`py-2.5 px-3 text-right font-mono font-semibold ${pnlColorClass(pos.unrealized)}`}>
                      {fmtSigned(pos.unrealized, 4)}
                    </td>
                    <td className={`py-2.5 px-3 text-right font-mono ${pnlColorClass(pos.realized)}`}>
                      {fmtSigned(pos.realized, 4)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-[#8B96A8]">
                      {pos.fees.toFixed(4)}
                    </td>
                    <td className="py-2.5 px-4 text-right">
                      <div className="inline-flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => setManualTarget(pos)}
                          className="px-2 py-1 text-[11px] rounded-[3px] border border-[#262F3F] bg-[#0B0E14] text-[#CBD5E1] hover:border-[#F59E0B] hover:text-[#F59E0B] cursor-pointer"
                        >
                          已手动平仓
                        </button>
                        <button
                          type="button"
                          onClick={() => setClosingPos(pos)}
                          className="px-2 py-1 text-[11px] rounded-[3px] border border-[#EF4444]/40 bg-[#EF4444]/10 text-[#EF4444] hover:bg-[#EF4444] hover:text-white cursor-pointer"
                        >
                          市价平仓
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Recorded Manual Closes Ledger */}
      {view.manual_closes.length > 0 && (
        <div className="cockpit-panel">
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#1E2634] bg-[#0E121A]">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-4 h-4 text-[#10B981]" />
              <h3 className="text-[12.5px] font-semibold text-[#E5E7EB]">
                MANUAL CLOSE SETTLEMENTS · 已登记手工平仓 ({view.manual_closes.length})
              </h3>
            </div>
          </div>
          <div className="divide-y divide-[#1E2634]">
            {view.manual_closes.map((mc) => (
              <div
                key={`${mc.pair}-${mc.side}`}
                className="flex items-center justify-between px-4 py-2 text-[12px] font-mono"
              >
                <div className="flex items-center gap-3">
                  <span className="font-semibold text-[#E5E7EB]">{mc.pair}</span>
                  <span className={mc.side === "BUY" ? "text-[#10B981]" : "text-[#EF4444]"}>
                    {mc.side}
                  </span>
                  <span className="text-[#8B96A8]">
                    Qty {mc.amount} @ {fmtPrice(mc.breakeven)}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className={`font-semibold ${pnlColorClass(mc.pnl)}`}>
                    {fmtSigned(mc.pnl, 4)} USDT
                  </span>
                  <button
                    type="button"
                    onClick={() => onDeleteManualClose(mc.pair, mc.side)}
                    className="inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#EF4444] hover:border-[#EF4444]/40 cursor-pointer"
                    title="撤销此条手动平仓记录"
                  >
                    <Trash2 className="w-3 h-3" />
                    <span>撤销</span>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Open Limit Orders */}
      <div className="cockpit-panel">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#1E2634]">
          <div className="flex items-center gap-2">
            <ListOrdered className="w-4 h-4 text-[#F59E0B]" />
            <h2 className="text-[13px] font-semibold text-[#E5E7EB]">
              OPEN LIMIT ORDERS · 活跃限价挂单 ({view.orders.length})
            </h2>
          </div>
          <span className="text-[11px] text-[#8B96A8]">待成交 Post-Only 入场或回归限价单</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-[#1E2634] bg-[#0E121A] text-[10.5px] font-mono uppercase text-[#8B96A8]">
                <th className="py-2.5 px-4">Pair</th>
                <th className="py-2.5 px-3">Side</th>
                <th className="py-2.5 px-3 text-right">Limit Price</th>
                <th className="py-2.5 px-3 text-right">Amount</th>
                <th className="py-2.5 px-4 text-right">Order Age</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E2634]/70">
              {view.orders.length === 0 ? (
                <tr>
                  <td colSpan={5} className="py-6 text-center text-[#8B96A8]">
                    当前暂无待成交限价挂单
                  </td>
                </tr>
              ) : (
                view.orders.map((ord, idx) => (
                  <tr key={`${ord.pair}-${idx}`} className="hover:bg-[#19202D]/60">
                    <td className="py-2 px-4">
                      <CoinBadge symbol={ord.symbol} pair={ord.pair} compact />
                    </td>
                    <td className={`py-2 px-3 font-mono font-semibold ${ord.side === "BUY" ? "text-[#10B981]" : "text-[#EF4444]"}`}>
                      {ord.side}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-[#E5E7EB]">{fmtPrice(ord.price)}</td>
                    <td className="py-2 px-3 text-right font-mono text-[#CBD5E1]">{ord.amount}</td>
                    <td className="py-2 px-4 text-right font-mono text-[#8B96A8]">{ord.age}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Position Executors Monitor */}
      <div className="cockpit-panel">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-[#1E2634]">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-[#38BDF8]" />
            <div>
              <h2 className="text-[13px] font-semibold text-[#E5E7EB]">
                POSITION EXECUTORS LIFECYCLE · 执行器生命周期 ({filteredExecutors.length})
              </h2>
              <p className="text-[11px] text-[#8B96A8]">跟踪每笔开单、ATR 动态止损、追踪止盈与平仓原因</p>
            </div>
          </div>
          <div className="inline-flex bg-[#0B0E14] border border-[#262F3F] rounded-[4px] p-0.5 text-[11px]">
            {(["all", "RUNNING", "TERMINATED"] as const).map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => setExecFilter(status)}
                className={`px-2.5 py-1 rounded-[3px] font-mono cursor-pointer ${
                  execFilter === status ? "bg-[#F59E0B]/15 text-[#F59E0B]" : "text-[#8B96A8] hover:text-[#E5E7EB]"
                }`}
              >
                {status === "all" ? "ALL 全部" : status}
              </button>
            ))}
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-[#1E2634] bg-[#0E121A] text-[10.5px] font-mono uppercase text-[#8B96A8]">
                <th className="py-2.5 px-4">Controller</th>
                <th className="py-2.5 px-3">Side</th>
                <th className="py-2.5 px-3">Status</th>
                <th className="py-2.5 px-3 text-right">Net PnL</th>
                <th className="py-2.5 px-3 text-right">PnL %</th>
                <th className="py-2.5 px-3 text-right">Volume</th>
                <th className="py-2.5 px-3 text-center">Trading</th>
                <th className="py-2.5 px-3">Close Type</th>
                <th className="py-2.5 px-4 text-right">Age</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E2634]/70">
              {filteredExecutors.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-6 text-center text-[#8B96A8]">
                    当前筛选条件下暂无执行器记录
                  </td>
                </tr>
              ) : (
                filteredExecutors.map((ex, idx) => (
                  <tr key={`${ex.controller}-${idx}`} className="hover:bg-[#19202D]/60">
                    <td className="py-2 px-4">
                      <CoinBadge symbol={ex.symbol} pair={ex.pair || ex.controller} compact />
                    </td>
                    <td className={`py-2 px-3 font-mono font-semibold ${ex.side === "BUY" ? "text-[#10B981]" : "text-[#EF4444]"}`}>
                      {ex.side}
                    </td>
                    <td className="py-2 px-3 font-mono">
                      <span
                        className={`px-1.5 py-0.5 text-[10px] rounded-[3px] border ${
                          ex.status === "RUNNING"
                            ? "bg-[#10B981]/15 text-[#10B981] border-[#10B981]/30"
                            : "bg-[#1E2634] text-[#8B96A8] border-[#262F3F]"
                        }`}
                      >
                        {ex.status}
                      </span>
                    </td>
                    <td className={`py-2 px-3 text-right font-mono ${pnlColorClass(ex.net_pnl)}`}>
                      {fmtSigned(ex.net_pnl, 4)}
                    </td>
                    <td className={`py-2 px-3 text-right font-mono ${pnlColorClass(ex.net_pnl_pct)}`}>
                      {(ex.net_pnl_pct * 100).toFixed(4)}%
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-[#CBD5E1]">
                      {ex.volume.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="py-2 px-3 text-center">
                      <span className={ex.is_trading ? "text-[#10B981]" : "text-[#475569]"}>
                        {ex.is_trading ? "●" : "○"}
                      </span>
                    </td>
                    <td className="py-2 px-3 font-mono text-[11px]">
                      {ex.close_type === "TAKE_PROFIT" ? (
                        <span className="px-1.5 py-0.5 rounded bg-[#10B981]/15 text-[#10B981]">TAKE_PROFIT</span>
                      ) : ex.close_type === "STOP_LOSS" ? (
                        <span className="px-1.5 py-0.5 rounded bg-[#EF4444]/15 text-[#EF4444]">STOP_LOSS</span>
                      ) : (
                        <span className="text-[#8B96A8]">{ex.close_type || "-"}</span>
                      )}
                    </td>
                    <td className="py-2 px-4 text-right font-mono text-[#8B96A8]">
                      {Math.round(ex.age)}s
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <ManualCloseModal
        open={Boolean(manualTarget)}
        pair={manualTarget?.pair || ""}
        side={manualTarget?.side || ""}
        onConfirm={async (pnl) => {
          if (!manualTarget) return;
          await onManualClose({
            pair: manualTarget.pair,
            side: manualTarget.side,
            amount: manualTarget.amount,
            breakeven: manualTarget.breakeven,
            pnl,
          });
          setManualTarget(null);
        }}
        onClose={() => setManualTarget(null)}
      />

      <ConfirmActionModal
        open={Boolean(closingPos)}
        title={`确认市价平仓 · ${closingPos?.pair || ""}`}
        description={`将向控制器 [${closingPos?.controller_id || ""}] 下发市价平仓请求并撤销挂单。确定继续吗？`}
        confirmLabel="确认平仓"
        tone="red"
        busy={busy}
        onConfirm={async () => {
          if (!closingPos) return;
          setBusy(true);
          try {
            await onCloseController(closingPos.controller_id);
          } finally {
            setBusy(false);
            setClosingPos(null);
          }
        }}
        onClose={() => setClosingPos(null)}
      />
    </div>
  );
};
