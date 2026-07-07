@echo off
pyinstaller --noconfirm --onefile --name house-admin ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  app.py

echo.
echo Build finished. Put config.ini next to dist\house-admin.exe
pause
