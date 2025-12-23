# RiskGuard - Setup simples (sem instalador .exe)

Este fluxo instala o Python necessario e as dependencias via pip para rodar o RiskGuard sem usar instalador.

## Como usar
1) Abra o PowerShell na pasta do projeto.
2) Execute:
```powershell
.\setup_riskguard.ps1
```

## O que o script faz
- Verifica Windows 64-bit e versao >= 10
- Baixa e instala o Python 3.10.11 (x64) se nao existir
- Cria um venv em `.\venv`
- Atualiza pip/setuptools/wheel
- Instala dependencias do `requirements.txt`
- Executa `health_check.py` (se existir)
- Escreve logs em `.\logs\setup.log`

## Opcoes
- Usar outra versao do Python:
```powershell
.\setup_riskguard.ps1 -PythonVersion 3.11.8
```
- Instalar para todos os usuarios (precisa admin):
```powershell
.\setup_riskguard.ps1 -InstallAllUsers
```
- Ignorar checagem de requirements fixas:
```powershell
.\setup_riskguard.ps1 -AllowUnpinned
```
- Pular health check:
```powershell
.\setup_riskguard.ps1 -SkipHealthCheck
```

## Execucao do app
Depois do setup:
```powershell
.\venv\Scripts\python.exe .\main.py
```

Ou use o atalho executavel:
```powershell
.\run_riskguard.bat
```

Se algo falhar, consulte `.\logs\setup.log`.
