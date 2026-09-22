Unicode true
!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "1.1.1"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef LAUNCHER
  !error "LAUNCHER is required"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "."
!endif

Name "OKX Quant Trader ${VERSION}"
OutFile "${OUTPUT_DIR}\OKX-Quant-Trader-${VERSION}-Windows-x64-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\OKX Quant Trader"
InstallDirRegKey HKCU "Software\OKX Quant Trader" "InstallDir"
RequestExecutionLevel user

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\start.cmd"
!define MUI_FINISHPAGE_RUN_TEXT "启动 OKX Quant Trader"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "OKX Quant Trader" SecMain
  SetOutPath "$INSTDIR"
  File /oname=OKXQuantTrader.exe "${LAUNCHER}"
  File "${SOURCE_DIR}\native\windows\start.cmd"
  File "${SOURCE_DIR}\native\windows\stop.cmd"
  File "${SOURCE_DIR}\native\windows\status.cmd"
  File "${SOURCE_DIR}\native\windows\replace-keys.cmd"
  SetOutPath "$INSTDIR\payload"
  File /r "${SOURCE_DIR}\*"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\OKX Quant Trader" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OKX Quant Trader" "DisplayName" "OKX Quant Trader"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OKX Quant Trader" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OKX Quant Trader" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  CreateDirectory "$SMPROGRAMS\OKX Quant Trader"
  CreateShortcut "$SMPROGRAMS\OKX Quant Trader\启动 OKX Quant Trader.lnk" "$INSTDIR\start.cmd" "" "$INSTDIR\OKXQuantTrader.exe"
  CreateShortcut "$SMPROGRAMS\OKX Quant Trader\停止 OKX Quant Trader.lnk" "$INSTDIR\stop.cmd" "" "$INSTDIR\OKXQuantTrader.exe"
  CreateShortcut "$SMPROGRAMS\OKX Quant Trader\查看状态.lnk" "$INSTDIR\status.cmd" "" "$INSTDIR\OKXQuantTrader.exe"
  CreateShortcut "$SMPROGRAMS\OKX Quant Trader\更新 OKX API.lnk" "$INSTDIR\replace-keys.cmd" "" "$INSTDIR\OKXQuantTrader.exe"
  CreateShortcut "$DESKTOP\OKX Quant Trader.lnk" "$INSTDIR\start.cmd" "" "$INSTDIR\OKXQuantTrader.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\OKX Quant Trader.lnk"
  RMDir /r "$SMPROGRAMS\OKX Quant Trader"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OKX Quant Trader"
  DeleteRegKey HKCU "Software\OKX Quant Trader"
  MessageBox MB_OK "程序已卸载。为防止误删密钥和日志，$LOCALAPPDATA\OKX Quant Trader 中的本机数据已保留。"
SectionEnd
