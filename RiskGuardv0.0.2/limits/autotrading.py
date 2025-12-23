import MetaTrader5 as mt5
import win32gui
import win32api
import win32con
import time

# Inicialize a conexão com o MT5
if not mt5.initialize():
    print("Falha ao inicializar MT5")
else:
    info = mt5.terminal_info()
    account = mt5.account_info()
    if account is None:
        print("Falha ao obter info da conta")
    else:
        print(f"Conta detectada: Login {account.login}, Server {account.server}")
        print(f"Estado atual de auto trading: {'Ativado' if info.trade_allowed else 'Desativado'}")
        
        if info.trade_allowed:
            login_str = str(account.login)
            server = account.server
            
            def enum_callback(hwnd, results):
                title = win32gui.GetWindowText(hwnd)
                if title and login_str in title and server in title:
                    results.append((hwnd, title))
            
            hwnds = []
            win32gui.EnumWindows(enum_callback, hwnds)
            
            if hwnds:
                hwnd, title = hwnds[0]  # Assume o primeiro encontrado é o principal
                print(f"Janela encontrada com título: {title}")
                # Comando correto para toggle auto trading em MT5
                win32api.PostMessage(hwnd, win32con.WM_COMMAND, 32851, 0)
                
                # Esperar um pouco para a mudança surtir efeito
                time.sleep(1)
                
                # Verificar estado após alteração
                mt5.shutdown()
                if mt5.initialize():
                    new_info = mt5.terminal_info()
                    print(f"Novo estado de auto trading: {'Ativado' if new_info.trade_allowed else 'Desativado'}")
                    mt5.shutdown()
                else:
                    print("Falha ao reinicializar MT5 para verificação")
            else:
                print("Janela do MT5 não encontrada")
        else:
            print("Auto trading já desativado")
    if mt5.last_error()[0] != 1:  # Se não estiver desligado ainda
        mt5.shutdown()
