#!/bin/bash
set -euo pipefail

VERSION="${1:-1.1.1}"
TARGET_ARCH="${2:-universal2}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_ROOT="${ROOT}/build/native-macos"
DIST_ROOT="${ROOT}/dist"
APP="${BUILD_ROOT}/dmg/OKX Quant Trader.app"
CONTENTS="${APP}/Contents"

rm -rf "${BUILD_ROOT}"
mkdir -p "${CONTENTS}/MacOS" "${CONTENTS}/Resources/bin" "${CONTENTS}/Resources/payload" "${DIST_ROOT}"
git -C "${ROOT}" archive --format=tar HEAD | tar -x -C "${CONTENTS}/Resources/payload"
cp "${ROOT}/native/macos/menu.sh" "${CONTENTS}/Resources/bin/menu.sh"
chmod +x "${CONTENTS}/Resources/bin/menu.sh"

python3 -m PyInstaller --clean --noconfirm --onefile --console \
  --target-architecture "${TARGET_ARCH}" \
  --name okx-quant-trader \
  --distpath "${CONTENTS}/Resources/bin" \
  --workpath "${BUILD_ROOT}/pyinstaller-work" \
  --specpath "${BUILD_ROOT}" \
  --hidden-import scripts.ensure_setup \
  --hidden-import dashboard.dashboard \
  --hidden-import dashboard.strategy_config \
  "${ROOT}/native/launcher.py"

cat > "${CONTENTS}/MacOS/OKX Quant Trader" <<'EOF'
#!/bin/bash
set -e
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="${CONTENTS}/Resources/bin/okx-quant-trader"
MENU="${CONTENTS}/Resources/bin/menu.sh"
COMMAND="$(printf '%q' "${MENU}") $(printf '%q' "${HELPER}")"
/usr/bin/osascript - "${COMMAND}" <<'APPLESCRIPT'
on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run
APPLESCRIPT
EOF
chmod +x "${CONTENTS}/MacOS/OKX Quant Trader"

cat > "${CONTENTS}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDisplayName</key><string>OKX Quant Trader</string>
  <key>CFBundleExecutable</key><string>OKX Quant Trader</string>
  <key>CFBundleIdentifier</key><string>com.bastnana.okx-quant-trader</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>OKX Quant Trader</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
EOF

ln -s /Applications "${BUILD_ROOT}/dmg/Applications"
codesign --force --deep --sign - "${APP}"
OUTPUT="${DIST_ROOT}/OKX-Quant-Trader-${VERSION}-macOS-Universal.dmg"
rm -f "${OUTPUT}"
hdiutil create -volname "OKX Quant Trader ${VERSION}" -srcfolder "${BUILD_ROOT}/dmg" \
  -ov -format UDZO "${OUTPUT}"
echo "Built ${OUTPUT}"
