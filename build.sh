#!/bin/bash
# build.sh — 构建 inspection-workflow 并清理 Docker 垃圾
#
# 流程：
#   1. 将当前代码提交到 git（自动生成 commit message）
#   2. 推送到 GitHub（确保代码有备份，随时可回退）
#   3. 清理旧的 Docker 构建缓存和 dangling 镜像
#   4. 构建并启动容器
#
# 用法：
#   ./build.sh              # 自动提交 + 构建
#   ./build.sh "修复了XX"    # 自定义 commit message
#   ./build.sh --no-push     # 只提交不推送（离线环境）
#   ./build.sh --no-clean    # 跳过 Docker 清理
#   ./build.sh --only-clean  # 只清理不构建

set -e

cd "$(dirname "$0")"

# ── 解析参数 ──────────────────────────────────────────────────────────
COMMIT_MSG=""
PUSH=true
CLEAN=true
BUILD=true

for arg in "$@"; do
  case "$arg" in
    --no-push)   PUSH=false ;;
    --no-clean)  CLEAN=false ;;
    --only-clean) BUILD=false; CLEAN=true ;;
    -*)          echo "未知参数: $arg"; exit 1 ;;
    *)           COMMIT_MSG="$arg" ;;
  esac
done

# ── Step 1: Git 提交 ──────────────────────────────────────────────────
if [ -n "$(git status --porcelain)" ]; then
  if [ -z "$COMMIT_MSG" ]; then
    # 自动生成 commit message：列出改动的文件类型
    CHANGED=$(git diff --name-only | head -10 | xargs -I{} basename {} | sed 's/\.[^.]*$//' | tr '\n' ', ' | sed 's/,$//')
    COMMIT_MSG="chore: 构建部署 — 更新 ${CHANGED} 等"
  fi
  echo "📦 提交代码: $COMMIT_MSG"
  git add -A
  git commit -m "$COMMIT_MSG"
else
  echo "✅ 代码无变更，跳过提交"
fi

# ── Step 2: 推送 GitHub ─────────────────────────────────────────────
if $PUSH; then
  echo "🚀 推送到 GitHub..."
  git push origin HEAD 2>&1 || echo "⚠️  推送失败，请检查网络或 SSH key"
else
  echo "⏭️  跳过推送 (--no-push)"
fi

# ── Step 3: 清理 Docker ─────────────────────────────────────────────
if $CLEAN; then
  echo "🧹 清理 Docker 旧缓存和镜像..."
  # 清理 48 小时前的构建缓存
  docker builder prune --filter "until=48h" --force 2>/dev/null || true
  # 清理 dangling 镜像（旧构建产生的 <none> 镜像）
  docker image prune --force 2>/dev/null || true
  echo "✅ 清理完成"
else
  echo "⏭️  跳过清理 (--no-clean)"
fi

# ── Step 4: 构建并启动 ──────────────────────────────────────────────
if $BUILD; then
  echo "🔨 构建并启动容器..."
  docker compose up -d --build
  echo ""
  echo "✅ 部署完成！"
  echo "   服务地址: http://localhost:8100"
  echo "   查看日志: docker compose logs -f app"
fi

# ── 提示当前版本 ─────────────────────────────────────────────────────
CURRENT_COMMIT=$(git rev-parse --short HEAD)
echo ""
echo "📌 当前版本: git-$CURRENT_COMMIT"
echo "   回退命令: git checkout <commit-hash> && ./build.sh"
