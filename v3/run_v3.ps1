param([int]$Port = 8513)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& py -3.13 -m streamlit run v3_app.py --server.address 127.0.0.1 --server.port $Port --server.headless true
