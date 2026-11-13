$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '缺少项目环境。请在项目目录运行 uv venv --python 3.11 .venv，然后用 uv pip install --python .venv\Scripts\python.exe -r requirements.txt 安装依赖。'
}
& $python -X utf8 (Join-Path $PSScriptRoot 'cli.py') @args
exit $LASTEXITCODE
