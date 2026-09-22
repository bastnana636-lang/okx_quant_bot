#!/bin/bash
set -u

HELPER="${1:?launcher path is required}"
while true; do
  clear
  echo "OKX Quant Trader"
  echo "================"
  echo "1) 启动机器人和 Dashboard"
  echo "2) 停止机器人"
  echo "3) 查看状态"
  echo "4) 查看实时日志"
  echo "5) 更新 OKX API 凭据"
  echo "0) 退出"
  echo
  read -r -p "请选择: " choice
  case "${choice}" in
    1) "${HELPER}" start ;;
    2) "${HELPER}" stop ;;
    3) "${HELPER}" status ;;
    4) "${HELPER}" logs ;;
    5) "${HELPER}" replace-keys ;;
    0) exit 0 ;;
    *) echo "无效选项。" ;;
  esac
  code=$?
  echo
  echo "操作结束（退出码 ${code}）。按回车键返回菜单。"
  read -r
done
