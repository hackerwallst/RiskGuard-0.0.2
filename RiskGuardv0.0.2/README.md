# RiskGuard

Monitor de risco para MetaTrader 5 que aplica limites operacionais, janela de noticias e relatorios de performance.

## Recursos
- Limite por trade e limite agregado
- Protecao de drawdown e kill switch
- Janela de noticias (calendario economico)
- Alertas por Telegram (opcional)
- Relatorios HTML/PDF e simulacoes Monte Carlo

## Requisitos
- Windows 10+ 64-bit
- MetaTrader 5 instalado
- Python 3.10+ (o script `setup_riskguard.ps1` instala 3.10.11)

## Configuracao
1) Copie `config.example.txt` para `config.txt`
2) Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` se for usar alertas
3) Ajuste os limites conforme sua operacao

`config.txt` nao deve ser commitado (esta no `.gitignore`).

## Instalacao rapida
```powershell
.\setup_riskguard.ps1
```

## Execucao
```powershell
.\run_riskguard.bat
```
ou
```powershell
.\venv\Scripts\python.exe .\main.py
```

## Relatorios
Arquivos gerados ficam em `reports/` (HTML, PDF, CSV, PNG). Eles estao ignorados no `.gitignore`.

## Estrutura do projeto
- `main.py`: loop principal
- `limits/`: limites, guardas e kill switch
- `news/`: janela de noticias
- `reports/`: relatorios e simulacoes
- `notify/`: alertas Telegram
- `logger/`: logs

## Publicar no GitHub
Se algum arquivo de cache ja estiver versionado (ex.: `news/ff_cache.json`), remova do indice:
```powershell
git rm --cached news/ff_cache.json
```

Depois, inicialize e envie:
```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/SEU_REPO.git
git push -u origin main
```

## Mais detalhes
Veja `README_setup.md` para o passo-a-passo completo do setup.
