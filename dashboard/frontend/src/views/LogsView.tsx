import React, { useEffect, useMemo, useRef, useState } from "react";
import { Copy, Download, Pause, Play, RefreshCw, Search, Terminal } from "lucide-react";
import { fetchLogs } from "../api";
import type { LogEntry } from "../types";

interface LogsViewProps {
  initialLogs: LogEntry[];
  onToast: (type: "ok" | "err" | "info", msg: string) => void;
}

export const LogsView: React.FC<LogsViewProps> = ({ initialLogs, onToast }) => {
  const [logs, setLogs] = useState<LogEntry[]>(initialLogs);
  const [level, setLevel] = useState<"ALL" | "ERROR" | "WARNING" | "INFO">("ALL");
  const [query, setQuery] = useState("");
  const [lineCount, setLineCount] = useState(250);
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const load = async () => {
    try {
      const res = await fetchLogs(lineCount);
      setLogs(res.lines);
    } catch {
      // keep existing
    }
  };

  useEffect(() => {
    load();
    if (paused) return;
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [lineCount, paused]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return logs.filter((entry) => {
      if (level !== "ALL" && entry.level !== level) return false;
      if (!q) return true;
      return entry.raw.toLowerCase().includes(q);
    });
  }, [logs, level, query]);

  useEffect(() => {
    if (autoScroll && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [filtered, autoScroll]);

  const counts = useMemo(() => {
    let err = 0;
    let warn = 0;
    for (const l of logs) {
      if (l.level === "ERROR") err++;
      else if (l.level === "WARNING") warn++;
    }
    return { err, warn };
  }, [logs]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(filtered.map((l) => l.raw).join("\n"));
      onToast("ok", `已复制 ${filtered.length} 行日志到剪贴板`);
    } catch {
      onToast("err", "剪贴板权限受限");
    }
  };

  const handleDownload = () => {
    const blob = new Blob([filtered.map((l) => l.raw).join("\n")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `okx-quant-logs-${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.log`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="cockpit-panel flex flex-col h-[calc(100vh-140px)] min-h-[480px]">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-[#1E2634] bg-[#0E121A]">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-[#F59E0B]" />
          <div>
            <h2 className="text-[13px] font-semibold text-[#E5E7EB]">
              CONSOLE LOG STREAM · 策略进程实时日志
            </h2>
            <p className="text-[11px] font-mono text-[#8B96A8]">
              显示 {filtered.length} / {logs.length} 行 ·{" "}
              <span className="text-[#EF4444]">{counts.err} ERRORS</span> ·{" "}
              <span className="text-[#F59E0B]">{counts.warn} WARNINGS</span>
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <div className="inline-flex bg-[#0B0E14] border border-[#262F3F] rounded-[4px] p-0.5 font-mono">
            {(["ALL", "ERROR", "WARNING", "INFO"] as const).map((lvl) => (
              <button
                key={lvl}
                type="button"
                onClick={() => setLevel(lvl)}
                className={`px-2 py-1 rounded-[3px] cursor-pointer ${
                  level === lvl
                    ? lvl === "ERROR"
                      ? "bg-[#EF4444]/20 text-[#EF4444]"
                      : lvl === "WARNING"
                      ? "bg-[#F59E0B]/20 text-[#F59E0B]"
                      : "bg-[#19202D] text-[#E5E7EB]"
                    : "text-[#8B96A8] hover:text-[#E5E7EB]"
                }`}
              >
                {lvl}
              </button>
            ))}
          </div>

          <div className="relative">
            <Search className="w-3.5 h-3.5 text-[#8B96A8] absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="过滤关键词..."
              className="cockpit-input pl-7 pr-2.5 py-1 w-40 font-mono"
            />
          </div>

          <select
            value={lineCount}
            onChange={(e) => setLineCount(Number(e.target.value))}
            className="cockpit-input px-2 py-1 font-mono"
          >
            <option value={120}>120 行</option>
            <option value={250}>250 行</option>
            <option value={500}>500 行</option>
          </select>

          <button
            type="button"
            onClick={() => setAutoScroll((v) => !v)}
            className={`px-2 py-1 rounded-[3px] border font-mono cursor-pointer ${
              autoScroll
                ? "border-[#10B981]/40 bg-[#10B981]/10 text-[#10B981]"
                : "border-[#262F3F] text-[#8B96A8]"
            }`}
          >
            AUTO-SCROLL: {autoScroll ? "ON" : "OFF"}
          </button>

          <button
            type="button"
            onClick={() => setPaused((v) => !v)}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-[3px] border border-[#262F3F] text-[#CBD5E1] hover:border-[#F59E0B] cursor-pointer"
          >
            {paused ? <Play className="w-3 h-3 text-[#10B981]" /> : <Pause className="w-3 h-3 text-[#F59E0B]" />}
            <span>{paused ? "继续" : "暂停"}</span>
          </button>

          <button
            type="button"
            onClick={load}
            className="p-1.5 rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
            title="立即刷新"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={handleCopy}
            className="p-1.5 rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
            title="复制全部"
          >
            <Copy className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={handleDownload}
            className="p-1.5 rounded-[3px] border border-[#262F3F] text-[#8B96A8] hover:text-[#F59E0B] cursor-pointer"
            title="导出 .log"
          >
            <Download className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div
        ref={boxRef}
        className="flex-1 overflow-y-auto p-3 bg-[#090B10] font-mono text-[11.5px] leading-relaxed space-y-0.5"
      >
        {filtered.length === 0 ? (
          <div className="py-12 text-center text-[#8B96A8]">暂无匹配的日志输出</div>
        ) : (
          filtered.map((entry) => {
            const cls =
              entry.level === "ERROR"
                ? "text-[#FCA5A5] bg-[#EF4444]/10 border-l-2 border-l-[#EF4444] pl-2"
                : entry.level === "WARNING"
                ? "text-[#FDE68A] bg-[#F59E0B]/5 border-l-2 border-l-[#F59E0B] pl-2"
                : "text-[#CBD5E1] pl-2.5";
            return (
              <div key={entry.id} className={`py-0.5 break-all whitespace-pre-wrap ${cls}`}>
                {entry.raw}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
