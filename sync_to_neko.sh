#!/bin/bash
# 一键同步 coyote_control 插件到 NEKO 运行目录

SRC="/Users/venlacy/Downloads/coyote_control"
DST="/Users/venlacy/Library/Application Support/N.E.K.O/plugins/coyote_control"
OFFICIAL="/Users/venlacy/Downloads/teai-regin/NEKO/official_plugins"

echo "🔄 开始同步..."

# 1. 同步到 NEKO 运行目录
echo "📁 同步到运行目录: $DST"
cp "$SRC/__init__.py" "$DST/__init__.py"
cp "$SRC/dglab_server.py" "$DST/dglab_server.py"
cp "$SRC/dglab_server_v4.py" "$DST/dglab_server_v4.py"
cp "$SRC/web_server.py" "$DST/web_server.py"
cp "$SRC/mood_engine.py" "$DST/mood_engine.py"
cp "$SRC/safety_limiter.py" "$DST/safety_limiter.py"
cp "$SRC/waveforms.py" "$DST/waveforms.py"
cp "$SRC/plugin.toml" "$DST/plugin.toml"
cp "$SRC/config.json.example" "$DST/config.json.example"
cp "$SRC/README.md" "$DST/README.md"
cp "$SRC/requirements.txt" "$DST/requirements.txt"
cp -r "$SRC/ui/." "$DST/ui/" 2>/dev/null
cp -r "$SRC/i18n/." "$DST/i18n/" 2>/dev/null
mkdir -p "$DST/docs" && cp -r "$SRC/docs/." "$DST/docs/" 2>/dev/null

# 2. 清理缓存
echo "🧹 清理 Python 缓存..."
find "$DST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find "$DST" -name "*.pyc" -delete 2>/dev/null

# 3. 打包到官方目录
echo "📦 打包到官方目录..."
cd "$(dirname "$SRC")"
VERSION=$(grep '^version' "$SRC/plugin.toml" | sed 's/.*"\(.*\)".*/\1/')
ZIP_NAME="coyote_control-${VERSION}.zip"
zip -r "$OFFICIAL/$ZIP_NAME" "coyote_control" \
    -x "*/.*" "*/__pycache__/*" "*.pyc" "*/data/*" "*/vendor/*" "*/sync_to_neko.sh" >/dev/null

echo "✅ 同步完成！"
echo ""
echo "📋 验证:"
echo "  - V4 服务器: $(ls -lh "$DST/dglab_server_v4.py" | awk '{print $5}')"
echo "  - 官方包: $ZIP_NAME ($(ls -lh "$OFFICIAL/$ZIP_NAME" | awk '{print $5}'))"
echo ""
echo "⚠️  请在 NEKO 中重载插件以生效"
