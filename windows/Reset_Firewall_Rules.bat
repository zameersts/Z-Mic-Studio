@echo off
:: Batch script to remove Python firewall rules so Windows prompts fresh on next launch
:: Must be run as Administrator
echo ============================================================
echo   Resetting Firewall Rules for Python and ZMic Studio
echo ============================================================
echo.

netsh advfirewall firewall delete rule name="pythonw.exe"
netsh advfirewall firewall delete rule name="python.exe"
netsh advfirewall firewall delete rule name="Python"

echo.
echo ============================================================
echo   Firewall rules have been completely removed!
echo   Windows will now prompt you fresh when ZMic Studio launches.
echo ============================================================
echo.
pause
