@echo off
chcp 65001 >nul
set /p QUERY=请输入企业名称、股票代码或统一社会信用代码（本次消耗1个Tavily credit）：
python tools\test_tavily.py "%QUERY%"
pause
