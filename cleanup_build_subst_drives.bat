@echo off
echo Removing OBS build SUBST drives R: through Z: if they map to this folder...
for %%D in (R S T U V W X Y Z) do (
    subst %%D: /D >nul 2>nul
)
echo Done.
pause
