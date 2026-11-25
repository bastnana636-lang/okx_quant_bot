import React, { useEffect, useState } from "react";
import { X, AlertTriangle } from "lucide-react";

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  maxWidthClass?: string;
}

export const Modal: React.FC<ModalProps> = ({
  open,
  title,
  onClose,
  children,
  maxWidthClass = "max-w-md",
}) => {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-[2px] p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={`w-full ${maxWidthClass} bg-[#141923] border border-[#262F3F] rounded-[4px] shadow-2xl overflow-hidden`}>
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#1E2634] bg-[#0E121A]">
          <h3 className="text-[13px] font-semibold tracking-wide text-[#E5E7EB]">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-[#8B96A8] hover:text-[#E5E7EB] p-1 rounded cursor-pointer"
            aria-label="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
};

interface ConfirmActionModalProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  tone?: "amber" | "red";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export const ConfirmActionModal: React.FC<ConfirmActionModalProps> = ({
  open,
  title,
  description,
  confirmLabel,
  tone = "amber",
  busy = false,
  onConfirm,
  onClose,
}) => (
  <Modal open={open} title={title} onClose={onClose}>
    <div className="flex items-start gap-3 mb-4">
      <div
        className={`p-2 rounded-[4px] border shrink-0 ${
          tone === "red"
            ? "bg-[#EF4444]/10 border-[#EF4444]/40 text-[#EF4444]"
            : "bg-[#F59E0B]/10 border-[#F59E0B]/40 text-[#F59E0B]"
        }`}
      >
        <AlertTriangle className="w-4 h-4" />
      </div>
      <p className="text-[12.5px] text-[#CBD5E1] leading-relaxed">{description}</p>
    </div>
    <div className="flex justify-end gap-2">
      <button
        type="button"
        onClick={onClose}
        className="px-3 py-1.5 text-[12px] font-medium rounded-[4px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] hover:bg-[#19202D] cursor-pointer"
      >
        取消 Cancel
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={onConfirm}
        className={`px-3 py-1.5 text-[12px] font-semibold rounded-[4px] cursor-pointer disabled:opacity-50 ${
          tone === "red"
            ? "bg-[#EF4444] hover:bg-[#DC2626] text-white"
            : "bg-[#F59E0B] hover:bg-[#D97706] text-[#0B0E14]"
        }`}
      >
        {busy ? "执行中..." : confirmLabel}
      </button>
    </div>
  </Modal>
);

interface ManualCloseModalProps {
  open: boolean;
  pair: string;
  side: string;
  onConfirm: (pnl: number) => void;
  onClose: () => void;
}

export const ManualCloseModal: React.FC<ManualCloseModalProps> = ({
  open,
  pair,
  side,
  onConfirm,
  onClose,
}) => {
  const [val, setVal] = useState("0.00");

  useEffect(() => {
    if (open) setVal("0.00");
  }, [open, pair]);

  const parsed = Number(val);
  const valid = val.trim() !== "" && Number.isFinite(parsed);

  return (
    <Modal open={open} title={`手动平仓盈亏录入 · ${pair} (${side})`} onClose={onClose}>
      <p className="text-[12px] text-[#8B96A8] leading-relaxed mb-3">
        若你已在 OKX 交易所端手动平掉该仓位，请填写实际结算净盈亏（USDT）。系统将从当前持仓隐藏该单并将净盈亏计入已实现与总盈亏。
      </p>
      <div className="flex items-center gap-2 mb-4">
        <input
          type="text"
          inputMode="decimal"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          placeholder="例如 1.25 或 -0.40"
          className="cockpit-input flex-1 px-3 py-2 text-[13px] font-mono"
          autoFocus
        />
        <span className="px-2.5 py-2 text-[11px] font-mono font-semibold bg-[#0E121A] border border-[#262F3F] rounded-[4px] text-[#F59E0B]">
          USDT
        </span>
      </div>
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-[12px] rounded-[4px] border border-[#262F3F] text-[#8B96A8] hover:text-[#E5E7EB] cursor-pointer"
        >
          取消
        </button>
        <button
          type="button"
          onClick={() => onConfirm(0)}
          className="px-3 py-1.5 text-[12px] rounded-[4px] border border-[#262F3F] bg-[#19202D] text-[#E5E7EB] hover:border-[#F59E0B]/50 cursor-pointer"
        >
          记为 0.00
        </button>
        <button
          type="button"
          disabled={!valid}
          onClick={() => valid && onConfirm(parsed)}
          className="px-3 py-1.5 text-[12px] font-semibold rounded-[4px] bg-[#F59E0B] hover:bg-[#D97706] text-[#0B0E14] disabled:opacity-40 cursor-pointer"
        >
          确认记账 Confirm
        </button>
      </div>
    </Modal>
  );
};
