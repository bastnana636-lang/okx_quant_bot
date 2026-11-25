import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Briefcase,
  CandlestickChart,
  LayoutGrid,
  Menu,
  Power,
  RotateCw,
  Settings,
  Terminal,
  X,
} from "lucide-react";
import {
  fetchConfig,
  fetchControlLog,
  fetchMarkets,
  fetchView,
  postClosePosition,
  postDeleteManualClose,
  postManualClose,
  postRestart,
  postStop,
} from "./api";
import type { ConfigResponse, DashboardView, MarketsResponse, TabKey } from "./types";
import { fmtAgeSeconds, fmtPrice, pnlColorClass } from "./utils/format";
import { OverviewView } from "./views/OverviewView";
import { MarketsView } from "./views/MarketsView";
import { PositionsView } from "./views/PositionsView";
import { ConfigView } from "./views/ConfigView";
import { LogsView } from "./views/LogsView";
import { ConfirmActionModal, Modal } from "./components/ConfirmModal";

interface ToastItem {
  id: number;
  type: "ok" | "err" | "info";
  msg: string;
}

const INITIAL_VIEW: DashboardView = {
  error: null,
  updated_at: 0,
  updated_str: "-",
  snapshot_age_s: null,
  uptime_s: 0,
  uptime_str: "-",
  running: false,
  stale: true,
  strategy: "-",
  strategy_info: {
    key: "momentum_1h_both",
    name: "策略初始化中",
    badge: "加载中",
    interval: "1h",
    mean_window: 16,
    time_limit: 604800,
    description: "",
  },
  side_mode: "both",
  control: { in_progress: false, action: null, pid: null },
  usdt_bal: 0,
  usdt_bal_str: "0.00",
  balances: {},
  realized: 0,
  unrealized: 0,
  global_pnl: 0,
  global_pct: "0.00%",
  total_volume: 0,
  session_peak: 0,
  session_min: 0,
  history: [],
  perf_rows: [],
  positions: [],
  orders: [],
  executors: [],
  signals: {},
  manual_closes: [],
  manual_close_note: "",
  warnings: [],
  logs: [],
};

function initialTab(): TabKey {
  const path = window.location.pathname.replace("/", "").toLowerCase();
  const hash = window.location.hash.replace("#", "").toLowerCase();
  const cand = (hash || path) as TabKey;
  if (["overview", "markets", "positions", "config", "logs"].includes(cand)) {
    return cand;
  }
  return "overview";
}

export const App: React.FC = () => {
  const [tab, setTab] = useState<TabKey>(initialTab);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [view, setView] = useState<DashboardView>(INITIAL_VIEW);
  const [markets, setMarkets] = useState<MarketsResponse | null>(null);
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [selectedPair, setSelectedPair] = useState("BTC-USDT");
  const [backendOffline, setBackendOffline] = useState(false);
  const [clock, setClock] = useState(() => new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  // Control modals & progress log modal
  const [confirmAction, setConfirmAction] = useState<"restart" | "stop" | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlLogModal, setControlLogModal] = useState<"start" | "stop" | null>(null);
  const [controlLogLines, setControlLogLines] = useState<string[]>([]);
  const [controlLogState, setControlLogState] = useState<{
    in_progress: boolean;
    exit_code: number | null;
  }>({ in_progress: false, exit_code: null });

  const pushToast = useCallback((type: "ok" | "err" | "info", msg: string) => {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    setToasts((prev) => [...prev.slice(-3), { id, type, msg }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4500);
  }, []);

  const refreshView = useCallback(async () => {
    try {
      const data = await fetchView();
      setView(data);
      setBackendOffline(false);
    } catch {
      setBackendOffline(true);
    }
  }, []);

  const refreshMarkets = useCallback(async () => {
    try {
      const data = await fetchMarkets();
      setMarkets(data);
    } catch {
      // keep existing cache
    }
  }, []);

  const refreshConfig = useCallback(async () => {
    try {
      const data = await fetchConfig();
      setConfig(data);
    } catch (err) {
      pushToast("err", err instanceof Error ? err.message : "读取策略配置失败");
    }
  }, [pushToast]);

  // Initial load + polling
  useEffect(() => {
    refreshView();
    refreshMarkets();
    refreshConfig();

    const viewTimer = window.setInterval(refreshView, 3000);
    const marketTimer = window.setInterval(refreshMarkets, 5000);
    const clockTimer = window.setInterval(() => {
      setClock(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    }, 1000);

    return () => {
      window.clearInterval(viewTimer);
      window.clearInterval(marketTimer);
      window.clearInterval(clockTimer);
    };
  }, [refreshView, refreshMarkets, refreshConfig]);

  // Control log polling when restart/stop progress modal is open
  useEffect(() => {
    if (!controlLogModal) return;
    let active = true;
    const pollControl = async () => {
      try {
        const res = await fetchControlLog(controlLogModal);
        if (active) {
          setControlLogLines(res.lines);
          setControlLogState({
            in_progress: res.control.in_progress,
            exit_code: res.control.exit_code,
          });
          if (!res.control.in_progress) {
            refreshView();
          }
        }
      } catch {
        // ignore
      }
    };
    pollControl();
    const timer = window.setInterval(pollControl, 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [controlLogModal]);

  const switchTab = (next: TabKey) => {
    setTab(next);
    setMobileNavOpen(false);
    window.history.replaceState(null, "", `#${next}`);
  };

  const openMarketPair = (pair: string) => {
    setSelectedPair(pair);
    switchTab("markets");
  };

  const handleControlConfirm = async () => {
    if (!confirmAction) return;
    setControlBusy(true);
    try {
      const res = confirmAction === "restart" ? await postRestart() : await postStop();
      pushToast(res.ok ? "ok" : "err", res.message);
      if (res.ok) {
        setControlLogModal(confirmAction === "restart" ? "start" : "stop");
      }
      refreshView();
    } catch (err) {
      pushToast("err", err instanceof Error ? err.message : "操作发送失败");
    } finally {
      setControlBusy(false);
      setConfirmAction(null);
    }
  };

  const handleCloseController = async (controller: string) => {
    try {
      const res = await postClosePosition(controller);
      pushToast("ok", res.message);
      refreshView();
    } catch (err) {
      pushToast("err", err instanceof Error ? err.message : "平仓指令发送失败");
    }
  };

  const handleManualClose = async (payload: {
    pair: string;
    side: string;
    amount: number;
    breakeven: number;
    pnl: number;
  }) => {
    try {
      const res = await postManualClose(payload);
      pushToast("ok", res.message);
      refreshView();
    } catch (err) {
      pushToast("err", err instanceof Error ? err.message : "手工平仓记账失败");
    }
  };

  const handleDeleteManualClose = async (pair: string, side: string) => {
    try {
      const res = await postDeleteManualClose(pair, side);
      pushToast("info", res.message);
      refreshView();
    } catch (err) {
      pushToast("err", err instanceof Error ? err.message : "撤销失败");
    }
  };

  // Top telemetry ticker pills (BTC, ETH, SOL)
  const topTickers = useMemo(() => {
    const wanted = ["BTC-USDT", "ETH-USDT", "SOL-USDT"];
    const byPair = new Map((markets?.tickers || []).map((t) => [t.pair, t]));
    return wanted.map((p) => byPair.get(p) || { pair: p, symbol: p.split("-")[0], last: null, change_pct: null });
  }, [markets]);

  const liveSnapshotAge = useMemo(() => {
    if (!view.updated_at) return null;
    return Math.max(0, Date.now() / 1000 - view.updated_at);
  }, [view.updated_at, clock]);

  const isLive = !backendOffline && !view.error && !view.stale && view.running;
  const errCount = useMemo(
    () => view.logs.filter((l) => l.level === "ERROR").length,
    [view.logs]
  );

  const navItems: Array<{
    id: TabKey;
    labelEn: string;
    labelZh: string;
    icon: React.ReactNode;
    badge?: string | number;
  }> = [
    { id: "overview", labelEn: "Overview", labelZh: "总览", icon: <LayoutGrid className="w-4 h-4" /> },
    { id: "markets", labelEn: "Markets", labelZh: "行情K线", icon: <CandlestickChart className="w-4 h-4" /> },
    {
      id: "positions",
      labelEn: "Positions",
      labelZh: "持仓监控",
      icon: <Briefcase className="w-4 h-4" />,
      badge: view.positions.length > 0 ? view.positions.length : undefined,
    },
    {
      id: "config",
      labelEn: "Config",
      labelZh: "策略配置",
      icon: <Settings className="w-4 h-4" />,
      badge: config ? `${config.pairs.length}` : undefined,
    },
    {
      id: "logs",
      labelEn: "Logs",
      labelZh: "日志",
      icon: <Terminal className="w-4 h-4" />,
      badge: errCount > 0 ? errCount : undefined,
    },
  ];

  const SidebarContent = (
    <div className="flex flex-col h-full bg-[#0E121A] border-r border-[#1E2634]">
      {/* Brand */}
      <div className="h-[52px] px-4 flex items-center justify-between border-b border-[#1E2634]">
        <div className="flex items-center gap-2.5">
          <svg width="24" height="24" viewBox="0 0 32 32" fill="none" className="shrink-0">
            <rect width="32" height="32" rx="4" fill="#141923" stroke="#262F3F" />
            <rect x="6" y="6" width="5.5" height="5.5" fill="#F59E0B" />
            <rect x="13.25" y="6" width="5.5" height="5.5" fill="#E5E7EB" />
            <rect x="20.5" y="6" width="5.5" height="5.5" fill="#E5E7EB" />
            <rect x="13.25" y="13.25" width="5.5" height="5.5" fill="#F59E0B" />
            <rect x="6" y="20.5" width="5.5" height="5.5" fill="#E5E7EB" />
            <rect x="20.5" y="20.5" width="5.5" height="5.5" fill="#F59E0B" />
          </svg>
          <div>
            <div className="text-[13px] font-bold tracking-wider text-[#E5E7EB]">OKX QUANT</div>
            <div className="text-[9.5px] font-mono text-[#F59E0B] tracking-widest">SLATE COCKPIT</div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setMobileNavOpen(false)}
          className="lg:hidden text-[#8B96A8] hover:text-[#E5E7EB]"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-3 px-2.5 space-y-1">
        {navItems.map((item) => {
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => switchTab(item.id)}
              className={`w-full flex items-center justify-between px-3 py-2.5 rounded-[4px] border-l-2 transition-colors cursor-pointer ${
                active
                  ? "bg-[#F59E0B]/12 border-l-[#F59E0B] text-[#F59E0B]"
                  : "border-l-transparent text-[#8B96A8] hover:text-[#E5E7EB] hover:bg-[#141923]"
              }`}
            >
              <div className="flex items-center gap-3">
                <span className={active ? "text-[#F59E0B]" : "text-[#8B96A8]"}>{item.icon}</span>
                <div className="text-left">
                  <div className={`text-[12.5px] font-semibold leading-tight ${active ? "text-[#F59E0B]" : "text-[#E5E7EB]"}`}>
                    {item.labelEn}
                  </div>
                  <div className="text-[10.5px] text-[#8B96A8] leading-tight mt-0.5">{item.labelZh}</div>
                </div>
              </div>
              {item.badge !== undefined && (
                <span
                  className={`px-1.5 py-0.5 text-[10px] font-mono rounded-[3px] ${
                    item.id === "logs"
                      ? "bg-[#EF4444]/15 text-[#EF4444]"
                      : active
                      ? "bg-[#F59E0B]/20 text-[#F59E0B]"
                      : "bg-[#19202D] text-[#8B96A8]"
                  }`}
                >
                  {item.badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Bottom Strategy Summary */}
      <div className="p-3 border-t border-[#1E2634] bg-[#0B0E14]/60 text-[11px]">
        <div className="flex items-center justify-between text-[#8B96A8] mb-1">
          <span>ACTIVE MODEL</span>
          <Activity className="w-3.5 h-3.5 text-[#F59E0B]" />
        </div>
        <div className="font-semibold text-[#E5E7EB] truncate">{view.strategy_info.name}</div>
        <div className="text-[10px] font-mono text-[#8B96A8] mt-1">
          {view.perf_rows.length} Pairs · Port 8888
        </div>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen flex bg-[#0B0E14] text-[#E5E7EB]">
      {/* Fixed Desktop Sidebar */}
      <aside className="hidden lg:block w-[208px] shrink-0 sticky top-0 h-screen">
        {SidebarContent}
      </aside>

      {/* Mobile Sidebar Drawer */}
      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-50 lg:hidden bg-black/70"
          onClick={(e) => {
            if (e.target === e.currentTarget) setMobileNavOpen(false);
          }}
        >
          <div className="w-[220px] h-full">{SidebarContent}</div>
        </div>
      )}

      {/* Main Column */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top Live Telemetry & Control Header */}
        <header className="sticky top-0 z-30 h-[52px] px-3 sm:px-4 bg-[#0E121A]/95 backdrop-blur-sm border-b border-[#1E2634] flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 overflow-x-auto no-scrollbar">
            <button
              type="button"
              onClick={() => setMobileNavOpen(true)}
              className="lg:hidden p-1.5 rounded-[4px] border border-[#262F3F] text-[#CBD5E1]"
              aria-label="打开导航"
            >
              <Menu className="w-4 h-4" />
            </button>

            {/* Live Clickable Market Ticker Pills */}
            <div className="hidden md:flex items-center gap-2">
              {topTickers.map((t) => (
                <button
                  key={t.pair}
                  type="button"
                  onClick={() => openMarketPair(t.pair)}
                  className="inline-flex items-center gap-2 px-2.5 py-1 rounded-[4px] bg-[#141923] border border-[#262F3F] hover:border-[#F59E0B]/60 text-[11.5px] font-mono transition-colors cursor-pointer shrink-0"
                >
                  <span className="font-semibold text-[#8B96A8]">{t.symbol}</span>
                  <span className="text-[#E5E7EB] font-semibold">
                    {t.last !== null ? `$${fmtPrice(t.last)}` : "--"}
                  </span>
                  {t.change_pct !== null && (
                    <span className={pnlColorClass(t.change_pct)}>
                      {t.change_pct >= 0 ? "+" : ""}
                      {t.change_pct.toFixed(2)}%
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>

          {/* Right Engine Telemetry & Controls (NO 只做多/双向 toggle) */}
          <div className="flex items-center gap-2 shrink-0">
            <div
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[4px] border text-[11px] font-mono font-semibold ${
                backendOffline
                  ? "bg-[#EF4444]/15 border-[#EF4444]/40 text-[#EF4444]"
                  : isLive
                  ? "bg-[#10B981]/15 border-[#10B981]/40 text-[#10B981]"
                  : view.running
                  ? "bg-[#F59E0B]/15 border-[#F59E0B]/40 text-[#F59E0B]"
                  : "bg-[#141923] border-[#262F3F] text-[#8B96A8]"
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  backendOffline
                    ? "bg-[#EF4444]"
                    : isLive
                    ? "bg-[#10B981] animate-pulse"
                    : view.running
                    ? "bg-[#F59E0B]"
                    : "bg-[#64748B]"
                }`}
              />
              <span>
                {backendOffline
                  ? "BACKEND OFFLINE"
                  : view.running
                  ? view.stale
                    ? "ON · STALE"
                    : "ON · LIVE"
                  : "ENGINE OFF"}
              </span>
            </div>

            <span className="hidden sm:inline-block text-[11px] font-mono text-[#8B96A8]">
              {liveSnapshotAge !== null ? fmtAgeSeconds(liveSnapshotAge) : "no snap"} · {clock}
            </span>

            <button
              type="button"
              onClick={() => setConfirmAction("restart")}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-[11.5px] font-medium rounded-[4px] border border-[#F59E0B]/40 bg-[#F59E0B]/10 text-[#F59E0B] hover:bg-[#F59E0B] hover:text-[#0B0E14] transition-colors cursor-pointer"
            >
              <RotateCw className="w-3 h-3" />
              <span>Restart 重启</span>
            </button>

            <button
              type="button"
              onClick={() => setConfirmAction("stop")}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-[11.5px] font-medium rounded-[4px] border border-[#EF4444]/40 bg-[#EF4444]/10 text-[#EF4444] hover:bg-[#EF4444] hover:text-white transition-colors cursor-pointer"
            >
              <Power className="w-3 h-3" />
              <span>Stop 停机</span>
            </button>
          </div>
        </header>

        {/* Main Workspace Canvas */}
        <main className="flex-1 p-3 sm:p-4 space-y-3">
          {/* Non-blocking Snapshot Stale / Offline Banner */}
          {(backendOffline || view.error || (view.stale && tab !== "config" && tab !== "markets")) && (
            <div className="cockpit-panel border-[#F59E0B]/40 bg-[#F59E0B]/10 px-3.5 py-2 flex flex-wrap items-center justify-between gap-2 text-[11.5px]">
              <div className="flex items-center gap-2 text-[#FDE68A]">
                <AlertTriangle className="w-4 h-4 text-[#F59E0B] shrink-0" />
                <span>
                  {backendOffline
                    ? "无法连接本地 Dashboard 后台服务 (127.0.0.1:8888)，正在自动重连..."
                    : view.error
                    ? `引擎状态快照暂不可用 (${view.error})。策略未启动时仍可正常浏览 OKX 行情 K 线与编辑策略配置。`
                    : `状态快照停留在 ${view.updated_str}（已超过 20s 未更新），实盘仓位请以交易所为准。`}
                </span>
              </div>
            </div>
          )}

          {tab === "overview" && (
            <OverviewView
              view={view}
              onOpenMarket={openMarketPair}
              onCloseController={handleCloseController}
            />
          )}
          {tab === "markets" && (
            <MarketsView
              view={view}
              markets={markets}
              selectedPair={selectedPair}
              onSelectPair={setSelectedPair}
              onRefreshMarkets={refreshMarkets}
              onCloseController={handleCloseController}
            />
          )}
          {tab === "positions" && (
            <PositionsView
              view={view}
              onCloseController={handleCloseController}
              onManualClose={handleManualClose}
              onDeleteManualClose={handleDeleteManualClose}
            />
          )}
          {tab === "config" && (
            <ConfigView
              config={config}
              onReloadConfig={refreshConfig}
              onToast={pushToast}
              onBotRestarted={() => setControlLogModal("start")}
            />
          )}
          {tab === "logs" && <LogsView initialLogs={view.logs} onToast={pushToast} />}
        </main>
      </div>

      {/* Confirm Restart / Stop Modal */}
      <ConfirmActionModal
        open={Boolean(confirmAction)}
        title={confirmAction === "restart" ? "确认重启量化策略引擎" : "确认停止量化策略引擎"}
        description={
          confirmAction === "restart"
            ? "重启将重新加载 conf/controllers/ 中的最新参数与交易对列表，并清零本机挂单操作盈亏基线（通常需 1–2 分钟）。确定重启？"
            : "停止命令会优雅终止当前策略运行。确定停止策略吗？"
        }
        confirmLabel={confirmAction === "restart" ? "确认重启 Restart" : "确认停机 Stop"}
        tone={confirmAction === "restart" ? "amber" : "red"}
        busy={controlBusy}
        onConfirm={handleControlConfirm}
        onClose={() => setConfirmAction(null)}
      />

      {/* Live Launcher Progress Log Modal */}
      <Modal
        open={Boolean(controlLogModal)}
        title={controlLogModal === "start" ? "策略重启进度日志 (dashboard/start.log)" : "策略停机日志 (dashboard/stop.log)"}
        onClose={() => setControlLogModal(null)}
        maxWidthClass="max-w-2xl"
      >
        <div className="flex items-center justify-between mb-2 text-[11.5px] font-mono">
          <span className="text-[#8B96A8]">
            当前阶段：
            {controlLogState.in_progress ? (
              <span className="text-[#F59E0B] ml-1">⏳ 正在执行启动/停止指令...</span>
            ) : controlLogState.exit_code === 0 ? (
              <span className="text-[#10B981] ml-1">✅ 执行成功 (exit 0)</span>
            ) : controlLogState.exit_code !== null ? (
              <span className="text-[#EF4444] ml-1">❌ 执行失败 (exit {controlLogState.exit_code})</span>
            ) : (
              <span className="text-[#CBD5E1] ml-1">已提交</span>
            )}
          </span>
        </div>
        <div className="bg-[#090B10] border border-[#1E2634] rounded-[4px] p-3 font-mono text-[11px] text-[#CBD5E1] h-64 overflow-y-auto space-y-0.5">
          {controlLogLines.length === 0 ? (
            <div className="text-[#8B96A8]">等待启动器日志输出...</div>
          ) : (
            controlLogLines.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">
                {line}
              </div>
            ))
          )}
        </div>
      </Modal>

      {/* Floating Toast Notifications */}
      <div className="fixed bottom-4 right-4 z-50 space-y-2 max-w-sm">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`px-3.5 py-2.5 rounded-[4px] border text-[12px] shadow-xl backdrop-blur-md ${
              t.type === "ok"
                ? "bg-[#0E121A]/95 border-[#10B981]/60 text-[#10B981]"
                : t.type === "err"
                ? "bg-[#0E121A]/95 border-[#EF4444]/60 text-[#FCA5A5]"
                : "bg-[#0E121A]/95 border-[#F59E0B]/60 text-[#FDE68A]"
            }`}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  );
};

export default App;
