import React, { useMemo, useState } from "react";
import type { HistoryPoint } from "../types";
import { fmtSigned, fmtUsd } from "../utils/format";

type ChartMode = "bot" | "assets" | "dual";

interface EquityChartProps {
  history: HistoryPoint[];
  botSessionPnl: number;
  sessionPeak: number;
  sessionMin: number;
  usdtBal: number;
}

export const EquityChart: React.FC<EquityChartProps> = ({
  history,
  botSessionPnl,
  sessionPeak,
  sessionMin,
  usdtBal,
}) => {
  const [mode, setMode] = useState<ChartMode>("bot");
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const W = 960;
  const H = 220;
  const PAD_L = 56;
  const PAD_R = 56;
  const PAD_T = 16;
  const PAD_B = 26;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;

  const series = useMemo(() => {
    if (!history || history.length === 0) {
      const now = Math.floor(Date.now() / 1000);
      return [
        { t: now - 60, bot_pnl: 0, assets: usdtBal },
        { t: now, bot_pnl: botSessionPnl, assets: usdtBal },
      ];
    }
    if (history.length === 1) {
      return [
        { ...history[0], t: history[0].t - 30 },
        history[0],
      ];
    }
    return history;
  }, [history, botSessionPnl, usdtBal]);

  const computed = useMemo(() => {
    const botVals = series.map((d) => d.bot_pnl);
    const assetVals = series.map((d) => d.assets);

    const bMinRaw = Math.min(0, ...botVals);
    const bMaxRaw = Math.max(0, ...botVals);
    const bPad = Math.max(0.25, (bMaxRaw - bMinRaw) * 0.15);
    const bMin = bMinRaw - bPad;
    const bMax = bMaxRaw + bPad;

    const aMinRaw = Math.min(...assetVals);
    const aMaxRaw = Math.max(...assetVals);
    const aPad = Math.max(1.0, (aMaxRaw - aMinRaw) * 0.18);
    const aMin = aMinRaw - aPad;
    const aMax = aMaxRaw + aPad;

    const xAt = (i: number) => PAD_L + (i / Math.max(1, series.length - 1)) * innerW;
    const yBot = (v: number) => PAD_T + ((bMax - v) / Math.max(1e-9, bMax - bMin)) * innerH;
    const yAssets = (v: number) => PAD_T + ((aMax - v) / Math.max(1e-9, aMax - aMin)) * innerH;

    const botPoints = series.map((d, i) => ({ x: xAt(i), y: yBot(d.bot_pnl), d }));
    const assetPoints = series.map((d, i) => ({ x: xAt(i), y: yAssets(d.assets), d }));

    const primaryPoints = mode === "assets" ? assetPoints : botPoints;
    const primaryLine = primaryPoints.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    const zeroY = mode === "assets" ? PAD_T + innerH : yBot(0);
    const baseAxisY = PAD_T + innerH;
    const primaryArea = `${primaryLine} L${primaryPoints[primaryPoints.length - 1].x.toFixed(1)},${baseAxisY.toFixed(1)} L${primaryPoints[0].x.toFixed(1)},${baseAxisY.toFixed(1)} Z`;

    const dualAssetLine = assetPoints.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

    const leftTicks = [0, 0.25, 0.5, 0.75, 1].map((frac) => {
      const y = PAD_T + frac * innerH;
      const val = mode === "assets" ? aMax - frac * (aMax - aMin) : bMax - frac * (bMax - bMin);
      return { y, label: mode === "assets" ? `$${val.toFixed(1)}` : `${val >= 0 ? "+" : ""}${val.toFixed(2)}` };
    });

    const rightTicks =
      mode === "dual"
        ? [0, 0.25, 0.5, 0.75, 1].map((frac) => ({
            y: PAD_T + frac * innerH,
            label: `$${(aMax - frac * (aMax - aMin)).toFixed(1)}`,
          }))
        : [];

    const xTickIndices = [0, Math.floor((series.length - 1) * 0.33), Math.floor((series.length - 1) * 0.66), series.length - 1]
      .filter((v, i, arr) => arr.indexOf(v) === i);
    const xTicks = xTickIndices.map((idx) => {
      const dt = new Date(series[idx].t * 1000);
      const label = dt.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
      return { x: xAt(idx), label };
    });

    return {
      botPoints,
      assetPoints,
      primaryPoints,
      primaryLine,
      primaryArea,
      dualAssetLine,
      zeroY,
      leftTicks,
      rightTicks,
      xTicks,
    };
  }, [series, mode, innerW, innerH]);

  const activePoint = hoverIdx !== null ? series[hoverIdx] : null;
  const activeCoord = hoverIdx !== null ? computed.primaryPoints[hoverIdx] : null;
  const latestPnl = series[series.length - 1]?.bot_pnl ?? botSessionPnl;
  const isPos = mode === "assets" ? true : latestPnl >= 0;

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * W;
    const norm = Math.max(0, Math.min(1, (relX - PAD_L) / innerW));
    const idx = Math.round(norm * (series.length - 1));
    setHoverIdx(idx);
  };

  return (
    <div className="cockpit-panel flex flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 border-b border-[#1E2634]">
        <div>
          <h2 className="text-[13px] font-semibold tracking-wide text-[#E5E7EB]">
            PORTFOLIO EQUITY &amp; BOT SESSION PNL · 账户净值与挂单盈亏曲线
          </h2>
          <p className="text-[11px] text-[#8B96A8] mt-0.5">
            本机挂单盈亏在策略重启时自动清零重算，仅重启面板保留历史基线
          </p>
        </div>

        <div className="inline-flex items-center bg-[#0B0E14] border border-[#262F3F] rounded-[4px] p-0.5 gap-0.5">
          {(
            [
              { id: "bot", label: "BOT PNL 挂单盈亏" },
              { id: "assets", label: "ASSETS 总资产" },
              { id: "dual", label: "DUAL 双轨对比" },
            ] as const
          ).map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setMode(tab.id)}
              className={`px-2.5 py-1 text-[11px] font-medium rounded-[3px] transition-colors cursor-pointer ${
                mode === tab.id
                  ? "bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/40"
                  : "text-[#8B96A8] hover:text-[#E5E7EB] border border-transparent"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div className="relative px-2 pt-2 pb-1">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="w-full h-[215px] select-none overflow-visible"
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          <defs>
            <linearGradient id="eqGradPos" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10B981" stopOpacity="0.32" />
              <stop offset="100%" stopColor="#10B981" stopOpacity="0.0" />
            </linearGradient>
            <linearGradient id="eqGradNeg" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#EF4444" stopOpacity="0.32" />
              <stop offset="100%" stopColor="#EF4444" stopOpacity="0.0" />
            </linearGradient>
            <linearGradient id="eqGradAmber" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#F59E0B" stopOpacity="0.28" />
              <stop offset="100%" stopColor="#F59E0B" stopOpacity="0.0" />
            </linearGradient>
          </defs>

          {/* Horizontal gridlines */}
          {computed.leftTicks.map((tick, idx) => (
            <g key={idx}>
              <line
                x1={PAD_L}
                y1={tick.y}
                x2={W - PAD_R}
                y2={tick.y}
                stroke="#1E2634"
                strokeWidth="1"
              />
              <text
                x={PAD_L - 8}
                y={tick.y + 3}
                textAnchor="end"
                className="fill-[#8B96A8] font-mono text-[10px]"
              >
                {tick.label}
              </text>
            </g>
          ))}

          {/* Right Y-axis ticks for Dual mode */}
          {computed.rightTicks.map((tick, idx) => (
            <text
              key={idx}
              x={W - PAD_R + 8}
              y={tick.y + 3}
              textAnchor="start"
              className="fill-[#F59E0B] font-mono text-[10px]"
            >
              {tick.label}
            </text>
          ))}

          {/* X-axis timestamps */}
          {computed.xTicks.map((xt, idx) => (
            <text
              key={idx}
              x={xt.x}
              y={H - 6}
              textAnchor="middle"
              className="fill-[#8B96A8] font-mono text-[10px]"
            >
              {xt.label}
            </text>
          ))}

          {/* Zero baseline */}
          {mode !== "assets" && (
            <line
              x1={PAD_L}
              y1={computed.zeroY}
              x2={W - PAD_R}
              y2={computed.zeroY}
              stroke="#334155"
              strokeDasharray="3 3"
              strokeWidth="1"
            />
          )}

          {/* Area & main line */}
          <path
            d={computed.primaryArea}
            fill={
              mode === "assets"
                ? "url(#eqGradAmber)"
                : isPos
                ? "url(#eqGradPos)"
                : "url(#eqGradNeg)"
            }
          />
          <path
            d={computed.primaryLine}
            fill="none"
            stroke={mode === "assets" ? "#F59E0B" : isPos ? "#10B981" : "#EF4444"}
            strokeWidth="2"
          />

          {/* Secondary assets line in dual mode */}
          {mode === "dual" && (
            <path
              d={computed.dualAssetLine}
              fill="none"
              stroke="#F59E0B"
              strokeWidth="1.6"
              strokeDasharray="4 2"
            />
          )}

          {/* Hover crosshair */}
          {activeCoord && (
            <>
              <line
                x1={activeCoord.x}
                y1={PAD_T}
                x2={activeCoord.x}
                y2={PAD_T + innerH}
                stroke="#F59E0B"
                strokeOpacity="0.6"
                strokeDasharray="2 2"
                strokeWidth="1"
              />
              <circle
                cx={activeCoord.x}
                cy={activeCoord.y}
                r="4"
                fill={mode === "assets" ? "#F59E0B" : isPos ? "#10B981" : "#EF4444"}
                stroke="#0B0E14"
                strokeWidth="1.5"
              />
            </>
          )}
        </svg>

        {activePoint && activeCoord && (
          <div
            className="pointer-events-none absolute top-3 bg-[#0B0E14]/95 border border-[#262F3F] rounded-[4px] px-2.5 py-1.5 text-[11px] font-mono shadow-xl"
            style={{
              left: activeCoord.x > W * 0.65 ? undefined : `${Math.min(72, (activeCoord.x / W) * 100 + 2)}%`,
              right: activeCoord.x > W * 0.65 ? `${Math.max(8, 100 - (activeCoord.x / W) * 100 + 2)}%` : undefined,
            }}
          >
            <div className="text-[#8B96A8] mb-0.5">
              {new Date(activePoint.t * 1000).toLocaleString("zh-CN", { hour12: false })}
            </div>
            <div className={activePoint.bot_pnl >= 0 ? "text-[#10B981]" : "text-[#EF4444]"}>
              Bot PnL: {fmtSigned(activePoint.bot_pnl, 4)} USDT
            </div>
            <div className="text-[#F59E0B]">Assets: {fmtUsd(activePoint.assets, false, 2)} USDT</div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 divide-y sm:divide-y-0 sm:divide-x divide-[#1E2634] border-t border-[#1E2634] bg-[#0E121A]/70 text-[11px]">
        <div className="px-4 py-2 flex items-center justify-between sm:block">
          <span className="text-[#8B96A8] block">SESSION BOT PNL 本次净值变化</span>
          <span className={`font-mono font-semibold ${botSessionPnl >= 0 ? "text-[#10B981]" : "text-[#EF4444]"}`}>
            {fmtSigned(botSessionPnl, 4)} USDT
          </span>
        </div>
        <div className="px-4 py-2 flex items-center justify-between sm:block">
          <span className="text-[#8B96A8] block">SESSION PEAK / MIN 峰值与谷值</span>
          <span className="font-mono font-semibold text-[#E5E7EB]">
            <span className="text-[#10B981]">{fmtSigned(sessionPeak, 2)}</span>
            <span className="text-[#4B5563] mx-1">/</span>
            <span className="text-[#EF4444]">{fmtSigned(sessionMin, 2)}</span>
          </span>
        </div>
        <div className="px-4 py-2 flex items-center justify-between sm:block">
          <span className="text-[#8B96A8] block">ACCOUNT EQUITY 总权益</span>
          <span className="font-mono font-semibold text-[#F59E0B]">{fmtUsd(usdtBal, false, 2)} USDT</span>
        </div>
        <div className="px-4 py-2 flex items-center justify-between sm:block">
          <span className="text-[#8B96A8] block">SAMPLES 采样点数</span>
          <span className="font-mono font-semibold text-[#E5E7EB]">{history.length} ticks</span>
        </div>
      </div>
    </div>
  );
};
