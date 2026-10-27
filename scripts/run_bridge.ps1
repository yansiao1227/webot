# MCP stdio入口；调用方工作目录为仓库根目录，Python从.env读取。
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = & python -B -X utf8 (Join-Path $repositoryRoot 'tool/settings.py') --get RE_WECHAT_PYTHON
if ($LASTEXITCODE -ne 0) { throw '读取Python配置失败' }
& $pythonPath -B -X utf8 (Join-Path $repositoryRoot 'wechat_mcp/bridge.py') @args
exit $LASTEXITCODE
