[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    Write-Host "[install-python3] $Message"
}

function Fail {
    param([string]$Message)
    throw "[install-python3] ERROR: $Message"
}

function Test-PythonReady {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        try {
            $version = & $python.Source --version 2>&1
            Write-Log "已检测到 $version，无需安装。"
            return $true
        } catch {
            return $false
        }
    }

    $python3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3) {
        try {
            $version = & $python3.Source --version 2>&1
            Write-Log "已检测到 $version，无需安装。"
            return $true
        } catch {
            return $false
        }
    }

    return $false
}

function Install-WithWinget {
    Write-Log "检测到 winget，正在安装 Python 3..."
    winget install --id Python.Python.3.12 --exact --accept-source-agreements --accept-package-agreements
}

function Install-WithChocolatey {
    Write-Log "检测到 Chocolatey，正在安装 Python 3..."
    choco install python -y
}

function Main {
    if (Test-PythonReady) {
        return
    }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Install-WithWinget
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        Install-WithChocolatey
    } else {
        Fail "未检测到 winget 或 Chocolatey。请先安装其中一个，或手动从 https://www.python.org/downloads/windows/ 安装 Python 3。"
    }

    if (Test-PythonReady) {
        Write-Log "安装完成。"
        Write-Log "可运行: python --version"
        return
    }

    Write-Log "安装流程已结束，但当前会话中仍未检测到 Python。"
    Write-Log "如果刚刚完成安装，请关闭并重新打开 PowerShell，再运行: python --version"
}

Main
