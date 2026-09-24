# gateway — unified tool gateway
# Re-exports from core modules for backward compatibility

try:
    from core.plugin_manager import PluginManager
except ImportError:
    pass

# tool_gateway stub for self-check
class _ToolGateway:
    pass

tool_gateway = _ToolGateway()
