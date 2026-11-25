import React from "react";

const COIN_ACCENTS: Record<string, string> = {
  BTC: "bg-amber-500/15 text-amber-400 border-amber-500/40",
  ETH: "bg-sky-500/15 text-sky-400 border-sky-500/40",
  SOL: "bg-emerald-500/15 text-emerald-400 border-emerald-500/40",
  XRP: "bg-slate-400/15 text-slate-200 border-slate-400/40",
  DOGE: "bg-yellow-500/15 text-yellow-400 border-yellow-500/40",
  SUI: "bg-cyan-500/15 text-cyan-400 border-cyan-500/40",
  SNDK: "bg-orange-500/15 text-orange-400 border-orange-500/40",
  ZEC: "bg-amber-400/15 text-amber-300 border-amber-400/40",
  OKB: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  ADA: "bg-blue-500/15 text-blue-400 border-blue-500/40",
  UNI: "bg-pink-500/15 text-pink-400 border-pink-500/40",
};

interface CoinBadgeProps {
  symbol: string;
  pair?: string;
  compact?: boolean;
}

export const CoinBadge: React.FC<CoinBadgeProps> = ({ symbol, pair, compact = false }) => {
  const clean = (symbol || pair?.split("-")[0] || "?").toUpperCase();
  const badgeStyle = COIN_ACCENTS[clean] || "bg-[#1F2937] text-[#E5E7EB] border-[#374151]";

  return (
    <div className="inline-flex items-center gap-2 min-w-0">
      <span
        className={`inline-flex items-center justify-center rounded-[4px] border font-mono font-semibold shrink-0 ${badgeStyle} ${
          compact ? "w-5 h-5 text-[10px]" : "w-6 h-6 text-[11px]"
        }`}
      >
        {clean.slice(0, 2)}
      </span>
      <span className="font-mono font-semibold text-[#E5E7EB] tracking-tight truncate">
        {pair || `${clean}-USDT`}
      </span>
    </div>
  );
};
