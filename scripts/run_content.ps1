# 从任意工作目录调用；Python路径由仓库.env注入。
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = & python -B -X utf8 (Join-Path $repositoryRoot 'tool/settings.py') --get RE_WECHAT_PYTHON
if ($LASTEXITCODE -ne 0) { throw '读取Python配置失败' }
& $pythonPath -B -X utf8 (Join-Path $repositoryRoot 'tool/read_content.py') @args
exit $LASTEXITCODE
