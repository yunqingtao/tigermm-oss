"""
Tiger.M.M Rich theme — replaces hand-coded ANSI escape codes.
Usage: from core.theme import console, GOLD, RED, GREEN, DIM
"""
from rich.console import Console
from rich.theme import Theme
from rich.style import Style

TIGER_THEME = Theme({
    "gold": "bold #FFAC02",
    "red": "bold #FF4444",
    "green": "bold #44FF44",
    "dim": "dim #888888",
    "cyan": "#00CCCC",
    "info": "italic #FFAC02",
})

console = Console(theme=TIGER_THEME)

# Style shortcuts for programmatic use
GOLD = Style(color="#FFAC02", bold=True)
RED = Style(color="#FF4444", bold=True)
GREEN = Style(color="#44FF44", bold=True)
DIM = Style(color="#888888", dim=True)
CYAN = Style(color="#00CCCC")

# Convenience functions
def gold(text): return f"[gold]{text}[/gold]"
def red(text): return f"[red]{text}[/red]"
def green(text): return f"[green]{text}[/green]"
def dim(text): return f"[dim]{text}[/dim]"

def print_gold(text): console.print(text, style="gold")
def print_red(text): console.print(text, style="red")
def print_green(text): console.print(text, style="green")
def print_dim(text): console.print(text, style="dim")

def rule(title=""):
    """Print a gold horizontal rule with optional title."""
    console.rule(f"[gold]{title}[/gold]" if title else "", style="gold")
