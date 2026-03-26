#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[install-python3] %s\n' "$*"
}

fail() {
  printf '[install-python3] ERROR: %s\n' "$*" >&2
  exit 1
}

require_sudo() {
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  elif [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    fail "需要管理员权限，请使用 sudo 运行该脚本。"
  fi
}

python_ready() {
  if command -v python3 >/dev/null 2>&1; then
    local version
    version="$(python3 --version 2>/dev/null || true)"
    log "已检测到 ${version:-python3}，无需安装。"
    return 0
  fi
  return 1
}

install_macos() {
  if python_ready; then
    return 0
  fi

  if command -v brew >/dev/null 2>&1; then
    log "检测到 Homebrew，正在安装 Python 3..."
    brew install python
    return 0
  fi

  fail "macOS 未检测到 Homebrew。请先安装 Homebrew，再重新运行本脚本。官网: https://brew.sh/"
}

install_linux() {
  if python_ready; then
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "检测到 apt-get，正在安装 Python 3..."
    require_sudo apt-get update
    require_sudo apt-get install -y python3 python3-venv python3-pip
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    log "检测到 dnf，正在安装 Python 3..."
    require_sudo dnf install -y python3 python3-pip
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    log "检测到 yum，正在安装 Python 3..."
    require_sudo yum install -y python3 python3-pip
    return 0
  fi

  if command -v zypper >/dev/null 2>&1; then
    log "检测到 zypper，正在安装 Python 3..."
    require_sudo zypper --non-interactive install python3 python3-pip
    return 0
  fi

  if command -v pacman >/dev/null 2>&1; then
    log "检测到 pacman，正在安装 Python 3..."
    require_sudo pacman -Sy --noconfirm python python-pip
    return 0
  fi

  if command -v apk >/dev/null 2>&1; then
    log "检测到 apk，正在安装 Python 3..."
    require_sudo apk add --no-cache python3 py3-pip
    return 0
  fi

  fail "未识别的 Linux 包管理器。请手动安装 python3。"
}

main() {
  local uname_out
  uname_out="$(uname -s 2>/dev/null || true)"

  case "${uname_out}" in
    Darwin)
      install_macos
      ;;
    Linux)
      install_linux
      ;;
    *)
      fail "该脚本仅支持 macOS 和 Linux。Windows 请使用 scripts/install_python3.ps1。"
      ;;
  esac

  if python_ready; then
    log "安装完成。"
    log "可运行: python3 --version"
    exit 0
  fi

  fail "安装流程已结束，但仍未检测到 python3。请检查系统 PATH 或重新打开终端。"
}

main "$@"
