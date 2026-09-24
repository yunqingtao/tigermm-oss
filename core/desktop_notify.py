"""
DesktopNotify — Windows toast notification for long-running tasks.
Uses PowerShell as fallback if win10toast not installed.
"""
import subprocess, logging, threading, re

logger = logging.getLogger("core.desktop_notify")


def notify(title: str, message: str, duration: str = "short"):
    """Show a Windows toast notification.
    Returns True if notification was shown, False otherwise.
    """
    # Clean text for PowerShell
    def clean(s):
        return s.replace('"', "'").replace("\n", " ").strip()[:200]

    t = clean(title)
    m = clean(message)

    # Method 1: win10toast (if installed)
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(t, m, duration=duration, threaded=True)
        return True
    except ImportError:
        pass

    # Method 2: PowerShell
    try:
        ps = f'''
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $template.GetElementsByTagName("text")[0].AppendChild($template.CreateTextNode("{t}")) > $null
        $template.GetElementsByTagName("text")[1].AppendChild($template.CreateTextNode("{m}")) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Tiger.M.M").Show($toast)
        '''
        subprocess.run(
            ["powershell", "-Command", ps],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return True
    except Exception:
        pass

    # Method 3: Simple msg box fallback
    try:
        subprocess.Popen(
            ["msg", "*", f"{t}\n{m}"],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return True
    except Exception:
        return False


def notify_async(title: str, message: str):
    """Non-blocking notification in background thread."""
    threading.Thread(target=notify, args=(title, message), daemon=True).start()
