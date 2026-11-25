import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import { RefreshCw, Layers, SlidersHorizontal, ArrowUpRight, ArrowDownRight } from "lucide-react";
import { fetchCandles } from "../api";
import type { CandlesResponse, DashboardView, MarketsResponse, PositionItem } from "../types";
import { fmtCompactVolume, fmtPrice, fmtSigned, pnlColorClass } from "../utils/format";
import { CoinBadge } from "../components/CoinBadge";
import { ConfirmActionModal } from "../components/ConfirmModal";

interface MarketsViewProps {
  view: DashboardView;
  markets: MarketsResponse | null;
  selectedPair: string;
  onSelectPair: (pair: string) => void;
  onRefreshMarkets: () => void;
  onCloseController: (controller: string) => Promise<void>;
}

const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h"] as const;

export const MarketsView: React.FC<MarketsViewProps> = ({
  view,
  markets,
  selectedPair,
  onSelectPair,
  onRefreshMarkets,
  onCloseController,
}) => {
  const [interval, setIntervalValue] = useState<string>("");
  const [candleData, setCandleData] = useState<CandlesResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [showBands, setShowBands] = useState<boolean>(true);
  const [showAtrStop, setShowAtrStop] = useState<boolean>(true);
  const [oscillatorMode, setOscillatorMode] = useState<"auto" | "z" | "momentum">("auto");
  const [hoverOhlc, setHoverOhlc] = useState<{ o: number; h: number; l: number; c: number; t?: string } | null>(null);
  const [closingPos, setClosingPos] = useState<PositionItem | null>(null);
  const [closeBusy, setCloseBusy] = useState(false);

  const mainChartRef = useRef<HTMLDivElement | null>(null);
  const subChartRef = useRef<HTMLDivElement | null>(null);

  const pairOptions = useMemo(() => {
    const list = markets?.tickers.map((t) => t.pair) || [];
    if (selectedPair && !list.includes(selectedPair)) list.unshift(selectedPair);
    return list.length > 0 ? list : ["BTC-USDT", "ETH-USDT", "SOL-USDT"];
  }, [markets, selectedPair]);

  // Load candlesticks when pair or interval changes
  const loadCandles = async (pair: string, tf?: string) => {
    setLoading(true);
    try {
      const res = await fetchCandles(pair, tf || undefined, 200);
      setCandleData(res);
      if (!tf && res.interval) {
        setIntervalValue(res.interval);
      }
    } catch {
      // handled in UI state
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCandles(selectedPair, interval || undefined);
  }, [selectedPair, interval]);

  // Effective oscillator display type
  const oscKind = useMemo(() => {
    if (oscillatorMode !== "auto") return oscillatorMode;
    return candleData?.settings.signal_mode === "momentum" ? "momentum" : "z";
  }, [oscillatorMode, candleData]);

  // Render TradingView Lightweight Charts (Main Candlestick + Synchronized Z-Score/Momentum Sub-Chart)
  useEffect(() => {
    const mainEl = mainChartRef.current;
    const subEl = subChartRef.current;
    if (!mainEl || !subEl || !candleData || candleData.candles.length === 0) return;

    const mainChart: IChartApi = createChart(mainEl, {
      layout: {
        background: { type: ColorType.Solid, color: "#0B0E14" },
        textColor: "#8B96A8",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#171D29" },
        horzLines: { color: "#171D29" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#F59E0B", width: 1, style: LineStyle.Dashed },
        horzLine: { color: "#F59E0B", width: 1, style: LineStyle.Dashed },
      },
      rightPriceScale: {
        borderColor: "#262F3F",
      },
      timeScale: {
        borderColor: "#262F3F",
        timeVisible: true,
        secondsVisible: false,
      },
      height: 360,
      width: mainEl.clientWidth,
    });

    const subChart: IChartApi = createChart(subEl, {
      layout: {
        background: { type: ColorType.Solid, color: "#0B0E14" },
        textColor: "#8B96A8",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#171D29" },
        horzLines: { color: "#171D29" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#F59E0B", width: 1, style: LineStyle.Dashed },
        horzLine: { color: "#64748B", width: 1, style: LineStyle.Dashed },
      },
      rightPriceScale: {
        borderColor: "#262F3F",
      },
      timeScale: {
        borderColor: "#262F3F",
        timeVisible: true,
        secondsVisible: false,
      },
      height: 130,
      width: subEl.clientWidth,
    });

    const candleSeries: ISeriesApi<"Candlestick"> = mainChart.addCandlestickSeries({
      upColor: "#10B981",
      downColor: "#EF4444",
      borderUpColor: "#10B981",
      borderDownColor: "#EF4444",
      wickUpColor: "#10B981",
      wickDownColor: "#EF4444",
    });

    const formattedCandles = candleData.candles.map((c) => ({
      time: c.time as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    candleSeries.setData(formattedCandles);

    if (showBands) {
      const meanSeries = mainChart.addLineSeries({
        color: "#F59E0B",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      meanSeries.setData(candleData.overlays.mean.map((p) => ({ time: p.time as Time, value: p.value })));

      const upperSeries = mainChart.addLineSeries({
        color: "rgba(56, 189, 248, 0.55)",
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      upperSeries.setData(candleData.overlays.upper_band.map((p) => ({ time: p.time as Time, value: p.value })));

      const lowerSeries = mainChart.addLineSeries({
        color: "rgba(56, 189, 248, 0.55)",
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      lowerSeries.setData(candleData.overlays.lower_band.map((p) => ({ time: p.time as Time, value: p.value })));
    }

    if (showAtrStop && candleData.overlays.atr_stop_long) {
      candleSeries.createPriceLine({
        price: candleData.overlays.atr_stop_long,
        color: "#EF4444",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `ATR Stop (-${((candleData.overlays.stop_pct || 0) * 100).toFixed(2)}%)`,
      });
    }

    if (candleData.active_position && candleData.active_position.breakeven > 0) {
      candleSeries.createPriceLine({
        price: candleData.active_position.breakeven,
        color: "#10B981",
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: `${candleData.active_position.side === "BUY" ? "LONG" : "SHORT"} Entry`,
      });
    }

    const oscSeries = subChart.addLineSeries({
      color: oscKind === "momentum" ? "#F59E0B" : "#38BDF8",
      lineWidth: 2,
      priceLineVisible: false,
    });

    const oscPoints =
      oscKind === "momentum" ? candleData.overlays.momentum_pct : candleData.overlays.z_score;
    oscSeries.setData(oscPoints.map((p) => ({ time: p.time as Time, value: p.value })));

    if (oscKind === "momentum") {
      const threshPct = candleData.settings.momentum_threshold * 100;
      oscSeries.createPriceLine({
        price: threshPct,
        color: "rgba(16, 185, 129, 0.75)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
        title: `+${threshPct.toFixed(2)}%`,
      });
      oscSeries.createPriceLine({
        price: -threshPct,
        color: "rgba(239, 68, 68, 0.75)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
        title: `-${threshPct.toFixed(2)}%`,
      });
    } else {
      const ez = candleData.settings.entry_z_score;
      oscSeries.createPriceLine({
        price: ez,
        color: "rgba(239, 68, 68, 0.75)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
        title: `+${ez}σ`,
      });
      oscSeries.createPriceLine({
        price: 0,
        color: "#475569",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
        title: "0",
      });
      oscSeries.createPriceLine({
        price: -ez,
        color: "rgba(16, 185, 129, 0.75)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
        title: `-${ez}σ`,
      });
    }

    // Sync time scales
    let syncing = false;
    mainChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      subChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });
    subChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      mainChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });

    mainChart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(candleSeries) as
        | { open: number; high: number; low: number; close: number }
        | undefined;
      if (bar) {
        setHoverOhlc({
          o: bar.open,
          h: bar.high,
          l: bar.low,
          c: bar.close,
          t: typeof param.time === "number" ? new Date(param.time * 1000).toLocaleString("zh-CN", { hour12: false }) : undefined,
        });
      } else {
        setHoverOhlc(null);
      }
    });

    mainChart.timeScale().fitContent();
    subChart.timeScale().fitContent();

    const ro = new ResizeObserver(() => {
      if (mainEl) mainChart.applyOptions({ width: mainEl.clientWidth });
      if (subEl) subChart.applyOptions({ width: subEl.clientWidth });
    });
    ro.observe(mainEl);
    ro.observe(subEl);

    return () => {
      ro.disconnect();
      mainChart.remove();
      subChart.remove();
    };
  }, [candleData, showBands, showAtrStop, oscKind]);

  const activeTicker = useMemo(
    () => markets?.tickers.find((t) => t.pair === selectedPair) || null,
    [markets, selectedPair]
  );

  const latestBar = candleData?.candles[candleData.candles.length - 1];
  const displayOhlc = hoverOhlc || (latestBar ? { o: latestBar.open, h: latestBar.high, l: latestBar.low, c: latestBar.close } : null);

  return (
    <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
      {/* Left 65% (8 cols): Interactive Candlestick + Z-Score / Momentum Oscillator */}
      <div className="xl:col-span-8 cockpit-panel overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 border-b border-[#1E2634] bg-[#0E121A]">
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={selectedPair}
              onChange={(e) => onSelectPair(e.target.value)}
              className="cockpit-input px-2.5 py-1 text-[12px] font-mono font-semibold text-[#F59E0B]"
            >
              {pairOptions.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>

            <div className="inline-flex items-center bg-[#0B0E14] border border-[#262F3F] rounded-[4px] p-0.5">
              {TIMEFRAMES.map((tf) => {
                const active = (interval || candleData?.interval || "1h") === tf;
                return (
                  <button
                    key={tf}
                    type="button"
                    onClick={() => setIntervalValue(tf)}
                    className={`px-2 py-0.5 text-[11px] font-mono rounded-[3px] cursor-pointer ${
                      active
                        ? "bg-[#F59E0B] text-[#0B0E14] font-semibold"
                        : "text-[#8B96A8] hover:text-[#E5E7EB]"
                    }`}
                  >
                    {tf.toUpperCase()}
                  </button>
                );
              })}
            </div>

            {candleData && (
              <span
                className={`px-1.5 py-0.5 text-[10px] font-mono rounded-[3px] border ${
                  candleData.source === "okx_live"
                    ? "bg-[#10B981]/10 text-[#10B981] border-[#10B981]/30"
                    : "bg-[#F59E0B]/10 text-[#F59E0B] border-[#F59E0B]/30"
                }`}
              >
                {candleData.source === "okx_live" ? "OKX LIVE" : candleData.source.toUpperCase()}
              </span>
            )}
          </div>

          <div className="flex items-center gap-1.5 text-[11px]">
            <button
              type="button"
              onClick={() => setShowBands((v) => !v)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-[3px] border cursor-pointer ${
                showBands
                  ? "border-[#F59E0B]/50 bg-[#F59E0B]/10 text-[#F59E0B]"
                  : "border-[#262F3F] text-[#8B96A8]"
              }`}
            >
              <Layers className="w-3 h-3" />
              <span>均值/通道</span>
            </button>
            <button
              type="button"
              onClick={() => setShowAtrStop((v) => !v)}
              className={`inline-flex items-center gap-1 px-2 py-1 rounded-[3px] border cursor-pointer ${
                showAtrStop
                  ? "border-[#EF4444]/50 bg-[#EF4444]/10 text-[#EF4444]"
                  : "border-[#262F3F] text-[#8B96A8]"
              }`}
            >
              <SlidersHorizontal className="w-3 h-3" />
              <span>ATR 止损线</span>
            </button>
            <button
              type="button"
              onClick={() => loadCandles(selectedPair, interval || undefined)}
              className="p-1.5 rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
              title="刷新 K 线"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin text-[#F59E0B]" : ""}`} />
            </button>
          </div>
        </div>

        {/* Live OHLC + Strategy Indicator Strip */}
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 border-b border-[#1E2634] text-[11px] font-mono bg-[#0B0E14]">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-[#8B96A8]">OKX Perpetual</span>
            {displayOhlc ? (
              <>
                <span className="text-[#E5E7EB]">
                  O <span className="text-[#CBD5E1]">{fmtPrice(displayOhlc.o)}</span>
                </span>
                <span className="text-[#E5E7EB]">
                  H <span className="text-[#10B981]">{fmtPrice(displayOhlc.h)}</span>
                </span>
                <span className="text-[#E5E7EB]">
                  L <span className="text-[#EF4444]">{fmtPrice(displayOhlc.l)}</span>
                </span>
                <span className="text-[#E5E7EB]">
                  C{" "}
                  <span className={displayOhlc.c >= displayOhlc.o ? "text-[#10B981]" : "text-[#EF4444]"}>
                    {fmtPrice(displayOhlc.c)}
                  </span>
                </span>
                {displayOhlc.t && <span className="text-[#8B96A8]">{displayOhlc.t}</span>}
              </>
            ) : (
              <span className="text-[#8B96A8]">加载 K 线中...</span>
            )}
          </div>
          {activeTicker?.change_pct !== null && activeTicker?.change_pct !== undefined && (
            <span className={pnlColorClass(activeTicker.change_pct)}>
              24H {activeTicker.change_pct >= 0 ? "+" : ""}
              {activeTicker.change_pct.toFixed(2)}% · Spread{" "}
              {activeTicker.spread_bps !== null ? `${activeTicker.spread_bps.toFixed(1)} bps` : "--"}
            </span>
          )}
        </div>

        {/* Main Candlestick Canvas */}
        <div className="relative bg-[#0B0E14]">
          {candleData && candleData.candles.length === 0 ? (
            <div className="h-[490px] flex flex-col items-center justify-center text-[#8B96A8] text-[12px] px-4 text-center">
              <p className="mb-1 text-[#E5E7EB]">暂未获取到 {selectedPair} K 线数据</p>
              <p className="text-[11px]">{candleData.error || "请检查网络/代理连接，或切换其他币种"}</p>
            </div>
          ) : (
            <>
              <div ref={mainChartRef} className="w-full" />
              <div className="flex items-center justify-between px-3 py-1.5 border-t border-b border-[#1E2634] bg-[#0E121A] text-[11px]">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono font-semibold text-[#E5E7EB]">
                    {oscKind === "momentum"
                      ? `MOMENTUM (${candleData?.settings.momentum_lookback ?? 16} bars %)`
                      : `Z-SCORE OSCILLATOR (${candleData?.settings.mean_window ?? 48} bars σ)`}
                  </span>
                  {candleData && (
                    <span className="font-mono text-[#8B96A8] text-[10.5px]">
                      阈值:{" "}
                      {oscKind === "momentum"
                        ? `±${(candleData.settings.momentum_threshold * 100).toFixed(2)}%`
                        : `±${candleData.settings.entry_z_score}σ`}
                    </span>
                  )}
                  {candleData?.overlays.stop_pct !== null && candleData?.overlays.stop_pct !== undefined && (
                    <span className="font-mono text-[#EF4444] text-[10.5px]">
                      ATR Stop: -{(candleData.overlays.stop_pct * 100).toFixed(2)}%
                    </span>
                  )}
                </div>
                <div className="inline-flex bg-[#0B0E14] border border-[#262F3F] rounded-[3px] p-0.5 text-[10px]">
                  {(
                    [
                      { id: "auto", label: "自动" },
                      { id: "z", label: "Z-Score" },
                      { id: "momentum", label: "Momentum%" },
                    ] as const
                  ).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setOscillatorMode(item.id)}
                      className={`px-2 py-0.5 rounded-[2px] cursor-pointer ${
                        oscillatorMode === item.id
                          ? "bg-[#F59E0B]/15 text-[#F59E0B]"
                          : "text-[#8B96A8] hover:text-[#E5E7EB]"
                      }`}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
              <div ref={subChartRef} className="w-full" />
            </>
          )}
        </div>
      </div>

      {/* Right 35% (4 cols): Stacked Market Watch & Spread + Active Positions */}
      <div className="xl:col-span-4 space-y-4">
        {/* Card 1: Market Watch & Spread */}
        <div className="cockpit-panel overflow-hidden">
          <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-[#1E2634] bg-[#0E121A]">
            <div>
              <h3 className="text-[12.5px] font-semibold tracking-wide text-[#E5E7EB]">
                MARKET WATCH &amp; SPREAD · 实时盘口价差
              </h3>
              <p className="text-[10.5px] text-[#8B96A8] font-mono">{markets?.meta || "加载行情中..."}</p>
            </div>
            <button
              type="button"
              onClick={onRefreshMarkets}
              className="p-1.5 rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#F59E0B] cursor-pointer"
              title="刷新盘口"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="max-h-[300px] overflow-y-auto">
            <table className="w-full text-left border-collapse text-[11.5px]">
              <thead>
                <tr className="border-b border-[#1E2634] bg-[#0B0E14] text-[10px] font-mono uppercase text-[#8B96A8]">
                  <th className="py-2 px-3">Pair</th>
                  <th className="py-2 px-2 text-right">Price</th>
                  <th className="py-2 px-2 text-right">Spread</th>
                  <th className="py-2 px-3 text-right">24h %</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1E2634]/70">
                {!markets || markets.tickers.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-6 text-center text-[#8B96A8]">
                      {markets?.error || "暂无 OKX 公开行情报价"}
                    </td>
                  </tr>
                ) : (
                  markets.tickers.map((t) => {
                    const isSelected = t.pair === selectedPair;
                    const wideSpread = (t.spread_pct ?? 0) > 0.2;
                    return (
                      <tr
                        key={t.pair}
                        onClick={() => onSelectPair(t.pair)}
                        className={`cursor-pointer transition-colors ${
                          isSelected
                            ? "bg-[#F59E0B]/10 border-l-2 border-l-[#F59E0B]"
                            : "hover:bg-[#19202D]/70"
                        }`}
                      >
                        <td className="py-2 px-3">
                          <div className="flex items-center justify-between gap-1">
                            <span className={`font-mono font-semibold ${isSelected ? "text-[#F59E0B]" : "text-[#E5E7EB]"}`}>
                              {t.pair}
                            </span>
                          </div>
                          <div className="text-[10px] font-mono text-[#8B96A8]">
                            Vol {fmtCompactVolume(t.vol_quote)}
                          </div>
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-[#E5E7EB]">
                          {fmtPrice(t.last)}
                        </td>
                        <td
                          className={`py-2 px-2 text-right font-mono ${
                            wideSpread ? "text-[#EF4444]" : "text-[#CBD5E1]"
                          }`}
                        >
                          {t.spread_bps !== null ? `${t.spread_bps.toFixed(1)} bps` : "--"}
                        </td>
                        <td className={`py-2 px-3 text-right font-mono font-medium ${pnlColorClass(t.change_pct)}`}>
                          {t.change_pct !== null ? (
                            <span className="inline-flex items-center justify-end gap-0.5">
                              {t.change_pct >= 0 ? (
                                <ArrowUpRight className="w-3 h-3" />
                              ) : (
                                <ArrowDownRight className="w-3 h-3" />
                              )}
                              {t.change_pct >= 0 ? "+" : ""}
                              {t.change_pct.toFixed(2)}%
                            </span>
                          ) : (
                            "--"
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Card 2: Active Positions & Executors */}
        <div className="cockpit-panel overflow-hidden">
          <div className="flex items-center justify-between gap-2 px-3.5 py-2.5 border-b border-[#1E2634] bg-[#0E121A]">
            <h3 className="text-[12.5px] font-semibold tracking-wide text-[#E5E7EB] truncate">
              ACTIVE POSITIONS &amp; EXECUTORS · 实时持仓
            </h3>
            <span className="text-[11px] font-mono text-[#F59E0B] whitespace-nowrap shrink-0">
              {view.positions.length} Open
            </span>
          </div>

          <div className="divide-y divide-[#1E2634]">
            {view.positions.length === 0 ? (
              <div className="py-6 px-4 text-center text-[11.5px] text-[#8B96A8]">
                当前无活跃保证金持仓，策略持续扫描信号中
              </div>
            ) : (
              view.positions.map((pos) => {
                const isBuy = pos.side === "BUY";
                const ticker = markets?.tickers.find((t) => t.pair === pos.pair);
                return (
                  <div
                    key={`${pos.pair}-${pos.side}`}
                    className="p-3 flex items-center justify-between gap-2 hover:bg-[#19202D]/50"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 mb-1">
                        <span
                          className={`px-1.5 py-0.5 text-[10px] font-mono font-bold rounded-[3px] ${
                            isBuy
                              ? "bg-[#10B981]/20 text-[#10B981]"
                              : "bg-[#EF4444]/20 text-[#EF4444]"
                          }`}
                        >
                          {isBuy ? "LONG" : "SHORT"}
                        </span>
                        <CoinBadge symbol={pos.symbol} pair={pos.pair} compact />
                        {pos.leverage > 0 && (
                          <span className="px-1.5 py-0.5 text-[10px] font-mono rounded-[3px] bg-[#F59E0B]/15 text-[#F59E0B] border border-[#F59E0B]/30">
                            {pos.leverage}x
                          </span>
                        )}
                      </div>
                      <div className="text-[10.5px] font-mono text-[#8B96A8]">
                        Entry {fmtPrice(pos.breakeven)} · Mark {fmtPrice(ticker?.last)} · Qty {pos.amount}
                      </div>
                    </div>

                    <div className="text-right shrink-0 flex items-center gap-2.5">
                      <div>
                        <div className={`text-[12.5px] font-mono font-semibold ${pnlColorClass(pos.unrealized)}`}>
                          {fmtSigned(pos.unrealized, 3)}
                        </div>
                        <div className="text-[10px] text-[#8B96A8] font-mono">USDT</div>
                      </div>
                      <button
                        type="button"
                        onClick={() => setClosingPos(pos)}
                        className="px-2 py-1 text-[11px] rounded-[3px] border border-[#262F3F] bg-[#0B0E14] text-[#CBD5E1] hover:border-[#EF4444] hover:text-[#EF4444] cursor-pointer"
                      >
                        Close
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      <ConfirmActionModal
        open={Boolean(closingPos)}
        title={`市价平仓确认 · ${closingPos?.pair || ""}`}
        description={`确定立即向控制器 [${closingPos?.controller_id || ""}] 下发市价平仓指令吗？`}
        confirmLabel="确认平仓"
        tone="red"
        busy={closeBusy}
        onConfirm={async () => {
          if (!closingPos) return;
          setCloseBusy(true);
          try {
            await onCloseController(closingPos.controller_id);
          } finally {
            setCloseBusy(false);
            setClosingPos(null);
          }
        }}
        onClose={() => setClosingPos(null)}
      />
    </div>
  );
};
