# 从仓库根目录运行；二进制、浏览器与登录文件均留在忽略的运行目录。
$ErrorActionPreference = 'Stop'
$platformRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../tool/state/platform-mcp'))
$platformPython = Join-Path $platformRoot 'venv/Scripts/python.exe'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $endpoint = & $platformPython -B -X utf8 -c "import os; from tool import settings; print(os.environ.get('CONTENT_XIAOHONGSHU_MCP_URL') or 'http://127.0.0.1:18060/mcp')"
    if ($LASTEXITCODE -ne 0) { throw '读取MCP配置失败' }
    $uri = [Uri]$endpoint
    $address = [Net.IPAddress]::Parse($uri.Host)
    if ($uri.Scheme -ne 'http' -or -not [Net.IPAddress]::IsLoopback($address) -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/mcp') {
        throw '小红书MCP必须使用回环HTTP地址和/mcp路径'
    }
    if (Get-NetTCPConnection -LocalAddress $uri.Host -LocalPort $uri.Port -State Listen -ErrorAction SilentlyContinue) {
        throw '目标端口已占用；请先确认现有服务，避免重复启动'
    }
    Start-Process -FilePath (Join-Path $platformRoot 'xiaohongshu-mcp.exe') -ArgumentList @('-port', $uri.Authority) -WorkingDirectory $platformRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $platformRoot 'xiaohongshu.stdout.log') -RedirectStandardError (Join-Path $platformRoot 'xiaohongshu.stderr.log')
    Write-Output '已启动小红书MCP；首次启动需要下载内置浏览器，查看tool/state/platform-mcp/xiaohongshu.stderr.log确认就绪。'
} finally {
    Pop-Location
}
