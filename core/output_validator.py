"""
Output Validator - JSON Schema, type check, dangerous pattern detection, semantic assertions.
"""
import json
import re
import os
from typing import Any, Dict, Tuple, Optional, Union, Callable

# Dangerous patterns (case-insensitive whole word or subcommand)
DEFAULT_DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\b',                    # rm -rf
    r'\bformat\s+[cC]:',                # format c:
    r'\bdd\s+if=',                      # dd if= 
    r'\bchmod\s+777\b',                 # chmod 777 (may be too broad, but include)
    r'>/dev/sda',                       # overwrite device
    r'\bDROP\s+TABLE\b',                # SQL drop table
    r'\bDELETE\s+FROM\b',               # SQL delete
    r'os\.remove\s*\(\s*[\'\"]?/etc/',  # OS remove critical files
    r'\bshutil\.rmtree\b',              # Python rmtree
    r'\bsubprocess\.call\(\s*[\'\"]\s*rm\s+-rf\b',
    r'\bexec\(\s*[\'\"]\s*rm\s+-rf\b',
    r'\b__import__\(\s*[\'\"]os[\'\"]\s*\)\.system\(\s*[\'\"]rm\s+-rf\b',
]

class ValidationResult:
    """Structured validation result"""
    def __init__(self, passed: bool, errors: list, warnings: list):
        self.passed = passed
        self.errors = errors
        self.warnings = warnings

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Validation [{status}]"]
        if self.errors:
            lines.append(f"Errors ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"  - {e}")
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  - {w}")
        return "\n".join(lines)

class OutputValidator:
    """Main validator class"""
    
    def __init__(self, 
                 schemas: Optional[Dict[str, dict]] = None,
                 type_contracts: Optional[Dict[str, dict]] = None,
                 semantic_checks: Optional[Dict[str, Callable]] = None,
                 dangerous_patterns: Optional[list] = None):
        """
        schemas: dict where key is content type id, value is JSON Schema dict.
        type_contracts: dict where key is content type id, value is dict of field: expected type.
        semantic_checks: dict where key is content type id, value is callable that receives parsed output and context.
        dangerous_patterns: list of regex strings to detect dangerous commands. Defaults to DEFAULT_DANGEROUS_PATTERNS.
        """
        self.schemas = schemas or {}
        self.type_contracts = type_contracts or {}
        self.semantic_checks = semantic_checks or {}
        self.dangerous_patterns = dangerous_patterns or list(DEFAULT_DANGEROUS_PATTERNS)
        self._total_validations = 0
        self._passed_validations = 0
    
    @property
    def pass_rate(self) -> float:
        """Return the ratio of passed validations to total validations.
        Returns 0.0 if no validations have been run yet."""
        if self._total_validations == 0:
            return 0.0
        return self._passed_validations / self._total_validations

    def validate(self, output_text: str, content_type: str = "default", context: dict = None) -> ValidationResult:
        """
        Validate an output text. Tries to parse as JSON if schema/type contract exists.
        Falls back to string checking for dangerous patterns and semantic checks.
        """
        errors = []
        warnings = []
        context = context or {}
        parsed = None
        
        # Try JSON parsing if schema or type contract present for this type
        schema = self.schemas.get(content_type)
        type_contract = self.type_contracts.get(content_type)
        if schema or type_contract:
            try:
                parsed = json.loads(output_text)
            except json.JSONDecodeError as e:
                errors.append(f"JSON parse error: {e}")
                # Even if JSON fails, we still do dangerous pattern check on raw text
                self._check_dangerous(output_text, errors)
                # Built-in semantic check on raw text
                semantic_errors = self._check_semantic(output_text, context)
                errors.extend(semantic_errors)
                return ValidationResult(False, errors, warnings)
        
        # JSON Schema validation
        if schema and parsed is not None:
            schema_errors = self._validate_json_schema(parsed, schema)
            errors.extend(schema_errors)
        
        # Type contract validation
        if type_contract and parsed is not None:
            type_errors = self._check_type_contract(parsed, type_contract)
            errors.extend(type_errors)
        
        # Dangerous pattern detection (run on raw text unless we have parsed and it's safe, 
        # but better always check raw text)
        self._check_dangerous(output_text, errors)
        
        # Built-in semantic assertions (contradiction detection)
        # Always runs on raw text; checks file/network claims against context reality
        semantic_errors = self._check_semantic(output_text, context)
        errors.extend(semantic_errors)
        
        # Custom semantic checks (registered per content_type)
        semantic_func = self.semantic_checks.get(content_type)
        if semantic_func:
            try:
                sem_result = semantic_func(parsed or output_text, context)
                if isinstance(sem_result, str):
                    errors.append(sem_result)
                elif isinstance(sem_result, list):
                    errors.extend(sem_result)
                elif isinstance(sem_result, tuple):
                    # (errors, warnings)
                    if sem_result[0]:
                        errors.extend(sem_result[0] if isinstance(sem_result[0], list) else [sem_result[0]])
                    if len(sem_result) > 1 and sem_result[1]:
                        warnings.extend(sem_result[1] if isinstance(sem_result[1], list) else [sem_result[1]])
            except Exception as e:
                errors.append(f"Semantic check raised exception: {e}")
        
        self._total_validations += 1
        passed = len(errors) == 0
        if passed:
            self._passed_validations += 1
        return ValidationResult(passed, errors, warnings)
    
    # ======================== Dangerous Pattern Management ========================
    
    def add_dangerous_pattern(self, pattern: str) -> bool:
        """
        Register a new dangerous pattern (regex string).
        
        Args:
            pattern: A valid regex pattern to detect dangerous output.
        
        Returns:
            True if the pattern was added, False if it was already registered.
        
        Raises:
            ValueError: If pattern is empty/None or not a valid regex.
        """
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"Pattern must be a non-empty string, got: {pattern!r}")
        
        # Validate regex by compiling it
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{pattern}': {e}")
        
        # Deduplicate: check if this exact pattern string already exists
        if pattern in self.dangerous_patterns:
            return False
        
        self.dangerous_patterns.append(pattern)
        return True
    
    def remove_dangerous_pattern(self, pattern: str) -> bool:
        """
        Remove a previously registered dangerous pattern.
        
        Returns:
            True if the pattern was found and removed, False if not found.
        """
        if pattern in self.dangerous_patterns:
            self.dangerous_patterns.remove(pattern)
            return True
        return False
    
    def clear_dangerous_patterns(self):
        """Remove all custom dangerous patterns, restoring only the module defaults."""
        self.dangerous_patterns = list(DEFAULT_DANGEROUS_PATTERNS)
    
    def get_dangerous_patterns(self) -> list:
        """Return a copy of the current dangerous patterns list."""
        return list(self.dangerous_patterns)
    
    # ======================== Internal Checks ========================
    
    def _check_dangerous(self, text: str, errors: list):
        """Check for dangerous patterns in text using instance-level patterns."""
        if not isinstance(text, str):
            return
        for pattern in self.dangerous_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                errors.append(f"Dangerous pattern detected: '{re.search(pattern, text).group()}'")
                break  # one is enough to fail
    
    def _check_semantic(self, output_text: str, context: dict) -> list:
        """
        Check for semantic contradictions between output text claims and context reality.
        
        Detects patterns like:
        - "deleted successfully" / "removed" but file still exists on disk
        - "file created" / "saved" / "wrote" but target path does not exist
        - "wrote N bytes" / "download complete" but file is empty (0 bytes)
        - "connected" / "sent" / "uploaded" but network is marked unavailable
        
        Args:
            output_text: The raw output text to scan for claims.
            context: Dict with optional keys:
                - target_file / file_path / path: file path to verify existence/size
                - require_network: bool, if True, network-related claims are checked
                - network_available: bool, if False, network claims raise errors
        
        Returns:
            List of error strings describing contradictions found.
        """
        errors = []
        
        if not isinstance(output_text, str) or not context:
            return errors
        
        # Resolve the target file path from context (support multiple key names)
        file_path = context.get('target_file') or context.get('file_path') or context.get('path')
        
        if file_path:
            file_path = str(file_path)
            
            # --- Deletion contradiction ---
            # Output claims "deleted/removed successfully" but file still exists
            if re.search(
                r'(?:deleted?|removed?|delet(?:ed|ion))\s+(?:success(?:fully)?|completed?|done|finished)',
                output_text, re.IGNORECASE
            ):
                if os.path.exists(file_path):
                    errors.append(
                        f"Semantic contradiction: output claims file deleted but '{file_path}' still exists"
                    )
            
            # --- Creation contradiction ---
            # Output claims "created/saved/wrote file" but path doesn't exist
            if re.search(
                r'(?:created?|saved?|wrote?|written?)\s+(?:success(?:fully)?|completed?|done|file|to\s+disk)',
                output_text, re.IGNORECASE
            ):
                if not os.path.exists(file_path):
                    errors.append(
                        f"Semantic contradiction: output claims file created but '{file_path}' does not exist"
                    )
            
            # --- Write size contradiction ---
            # Output claims "wrote N bytes" or "download complete" but file is empty
            if re.search(
                r'(?:wrote?\s+\d+\s+bytes?|download(?:ed)?\s+(?:complete|success|finished))',
                output_text, re.IGNORECASE
            ):
                if os.path.exists(file_path) and os.path.getsize(file_path) == 0:
                    errors.append(
                        f"Semantic contradiction: output claims data written but '{file_path}' is empty (0 bytes)"
                    )
        
        # --- Network contradiction ---
        # Output claims network activity but context says network is down
        if context.get('require_network') or context.get('network_available') is False:
            if re.search(
                r'(?:connected?|sent?|transmitted?|uploaded?|synced?)\s+(?:success(?:fully)?|completed?|ok)',
                output_text, re.IGNORECASE
            ):
                if context.get('network_available') is False:
                    errors.append(
                        "Semantic contradiction: output claims network activity but network is unavailable"
                    )
        
        return errors
    
    def _validate_json_schema(self, instance: Any, schema: dict, path: str = "$") -> list:
        """Basic and recursive JSON Schema validation (supports type, required, properties, additionalProperties, items)."""
        errors = []
        if not isinstance(schema, dict):
            return errors
        
        # type check
        expected_type = schema.get("type")
        if expected_type:
            if not self._match_type(instance, expected_type):
                errors.append(f"{path}: expected type {expected_type}, got {type(instance).__name__}")
                return errors  # can't continue deeper if type mismatch
        
        # if instance is a dict
        if isinstance(instance, dict) and schema.get("properties"):
            # required check
            required_fields = schema.get("required", [])
            for field in required_fields:
                if field not in instance:
                    errors.append(f"{path}: missing required field '{field}'")
            # additionalProperties
            additional = schema.get("additionalProperties", True)
            if not additional:
                allowed_props = set(schema.get("properties", {}).keys())
                for key in instance:
                    if key not in allowed_props:
                        errors.append(f"{path}: additional property '{key}' not allowed")
            # validate each property if schema defines it
            for prop, sub_schema in schema.get("properties", {}).items():
                if prop in instance:
                    sub_errors = self._validate_json_schema(instance[prop], sub_schema, f"{path}.{prop}")
                    errors.extend(sub_errors)
        
        # if instance is a list
        if isinstance(instance, list) and schema.get("items"):
            item_schema = schema["items"]
            if isinstance(item_schema, dict):
                for idx, item in enumerate(instance):
                    sub_errors = self._validate_json_schema(item, item_schema, f"{path}[{idx}]")
                    errors.extend(sub_errors)
            elif isinstance(item_schema, list):
                # tuple typing, validate up to length of list
                for idx in range(min(len(item_schema), len(instance))):
                    sub_errors = self._validate_json_schema(instance[idx], item_schema[idx], f"{path}[{idx}]")
                    errors.extend(sub_errors)
        
        # enum check
        if "enum" in schema:
            if instance not in schema["enum"]:
                errors.append(f"{path}: value {instance!r} not in allowed enum {schema['enum']}")
        
        return errors
    
    def _match_type(self, instance, json_type: str) -> bool:
        """Match Python value to JSON Schema type string."""
        if json_type == "object":
            return isinstance(instance, dict)
        elif json_type == "array":
            return isinstance(instance, list)
        elif json_type == "string":
            return isinstance(instance, str)
        elif json_type == "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        elif json_type == "number":
            return isinstance(instance, (int, float)) and not isinstance(instance, bool)
        elif json_type == "boolean":
            return isinstance(instance, bool)
        elif json_type == "null":
            return instance is None
        return True  # unknown type, pass
    
    def _check_type_contract(self, parsed: dict, contract: dict, path: str = "$") -> list:
        """Check that fields match declared Python types (e.g., int, str, list). Contract can use type objects or strings."""
        errors = []
        for field, expected in contract.items():
            if field not in parsed:
                errors.append(f"{path}: missing field '{field}' required by contract")
                continue
            value = parsed[field]
            # expected can be a type or string name
            if isinstance(expected, type):
                if not isinstance(value, expected):
                    errors.append(f"{path}.{field}: expected {expected.__name__}, got {type(value).__name__}")
            elif isinstance(expected, str):
                expected_lower = expected.lower()
                if expected_lower == "int" and not isinstance(value, int):
                    errors.append(f"{path}.{field}: expected int, got {type(value).__name__}")
                elif expected_lower == "str" and not isinstance(value, str):
                    errors.append(f"{path}.{field}: expected str, got {type(value).__name__}")
                elif expected_lower == "float" and not isinstance(value, (int, float)):
                    errors.append(f"{path}.{field}: expected float, got {type(value).__name__}")
                elif expected_lower == "list" and not isinstance(value, list):
                    errors.append(f"{path}.{field}: expected list, got {type(value).__name__}")
                elif expected_lower == "dict" and not isinstance(value, dict):
                    errors.append(f"{path}.{field}: expected dict, got {type(value).__name__}")
                elif expected_lower == "bool" and not isinstance(value, bool):
                    errors.append(f"{path}.{field}: expected bool, got {type(value).__name__}")
                elif expected_lower == "none":
                    if value is not None:
                        errors.append(f"{path}.{field}: expected None, got {type(value).__name__}")
                # else ignore unknown type string
        return errors


# ======================== Semantic assertion helpers (callable examples) ========================

def file_deleted_check(output_text: str, context: dict):
    """Check if output says 'deleted successfully' but file still exists."""
    if re.search(r'deleted?\s+success(fully)?', output_text, re.IGNORECASE):
        target_file = context.get('target_file')
        if target_file and os.path.exists(target_file):
            return f"Semantic: claimed deletion but file '{target_file}' still exists"
    return None  # no problem


def check_evidence(output_text: str) -> dict:
    """Check if response makes completion claims without supporting evidence.
    
    Returns:
        dict with keys: has_claim (bool), has_evidence (bool), 
        claim_patterns (list), evidence_found (list), passed (bool)
    """
    result = {
        "has_claim": False, "has_evidence": False,
        "claim_patterns": [], "evidence_found": [], "passed": True
    }
    
    # ── 1. Detect completion claims ("嘴炮"), skip negated ──
    claim_patterns = [
        (r'(?:已|已经)(?:完成|修复|修复好|搞定|做好|写好|改好|删掉|删除|移除|创建|添加)', '已完成/已修复'),
        (r'(?:done|fixed|finished|completed|resolved|solved)', 'done/fixed'),
        (r'(?:好了|行了|可以了|完工|收工)', '好了/完工'),
        (r'(?:没问题了|没毛病|搞定了)', '没问题/搞定'),
    ]
    for pattern, label in claim_patterns:
        for m in re.finditer(pattern, output_text, re.IGNORECASE):
            # Check context for negation (前后各10字)
            start = max(0, m.start() - 10)
            end = min(len(output_text), m.end() + 10)
            ctx = output_text[start:end]
            # 否定词在前面 OR "等于没说/不算数/不算/没意义" 等否定句
            negated = any(w in output_text[start:m.start()] for w in 
                         ['不', '没', '别', '不是', '不算', '并非', '别说', '不要', '不能', '不会'])
            negated = negated or any(phrase in ctx for phrase in 
                         ['等于没说', '不算数', '不算', '没意义', '不管用', '没用', '白搭'])
            if not negated:
                result["has_claim"] = True
                result["claim_patterns"].append(label)
                break  # one un-negated claim is enough
    
    if not result["has_claim"]:
        return result  # No completion claims, skip
    
    # ── 2. Check for evidence ──
    evidence_patterns = [
        (r'[a-fA-F0-9]{32}', 'MD5'),
        (r'\b\d+\s*(?:行|lines?|files?|个文件|个测试|条)', '行数/文件数'),
        (r'\b\d+\s*(?:passed|通过|OK|成功)', '测试结果'),
        (r'(?:pass|PASS|ok|OK|通过)\s*[:：]?\s*\d+', 'pass计数'),
        (r'[A-Z]:[/\\][^\s,，。]{3,}', '完整路径'),
        (r'[\w/-]+\.(?:py|txt|md|json|yaml|toml|cfg|ini)', '文件路径'),
        (r'\b\d+\s*(?:bytes?|KB|MB|GB|字节)', '文件大小'),
        (r'(?:输出|结果|output|result)\s*[:：]', '实际输出'),
        (r'(?:verified?|confirmed?|tested?|checked?)\s+(?:OK|ok|通过|成功|passed)', '验证确认'),
        (r'文件在(?:桌面|Desktop|文档|下载|Downloads)', '文件位置'),
    ]
    for pattern, label in evidence_patterns:
        if re.search(pattern, output_text, re.IGNORECASE):
            result["has_evidence"] = True
            result["evidence_found"].append(label)
    
    result["passed"] = not result["has_claim"] or result["has_evidence"]
    return result
