@echo off
chcp 65001 >nul
echo ==========================================
echo  Lit AI Collector - PyInstaller 打包脚本
echo ==========================================
echo.

pyinstaller LitAICollector.spec --clean --noconfirm

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] 打包失败，请检查上方错误信息。
    pause
    exit /b 1
)

echo.
echo [SUCCESS] 打包完成！
echo 输出目录: dist\LitAICollector\
echo 可执行文件: dist\LitAICollector\LitAICollector.exe
echo.
pause
